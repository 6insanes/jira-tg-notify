"""The poll loop: fetch issues, diff against stored state, notify, persist."""

from __future__ import annotations

import signal
import time
import urllib.error

from .config import (
    Config,
    MUTED_SETTING,
    UNMUTED_SETTING,
    effective_exclusions,
    normalize_project_key,
)
from .events import FIELD_EVENTS, detect_events
from .formatting import compose, compose_comment
from .jira import JiraClient
from .log import log
from .models import (
    IssueSnapshot,
    build_snapshot,
    display_name,
    epoch,
    max_comment_id,
    pick_new_comments,
    same_person,
)
from .storage import StateStore
from .telegram import TelegramClient

MIN_INTERVAL = 5
COMMAND_TIMEOUT = 20


class Poller:
    def __init__(
        self,
        cfg: Config,
        jira: JiraClient | None = None,
        telegram: TelegramClient | None = None,
        log_fn=log,
        commander=None,
    ):
        self.cfg = cfg
        self.jira = jira or JiraClient(cfg.jira)
        self.telegram = telegram or TelegramClient(cfg.telegram)
        self.log = log_fn
        self.commander = commander
        self._me: dict | None = None

    def me(self) -> dict:
        """The Jira user behind the token, cached; empty when it cannot be read."""
        if self._me is None:
            try:
                self._me = self.jira.myself() or {}
            except (RuntimeError, SystemExit, urllib.error.URLError) as err:
                self.log(
                    f"cannot detect the current Jira user ({err}): "
                    "own actions will still notify"
                )
                self._me = {}
        return self._me

    def poll_once(self, dry_run: bool = False, verbose: bool = False) -> int:
        """Run one poll and return the number of messages sent."""
        jcfg = self.cfg.jira
        with StateStore(
            jcfg.state_db, jcfg.legacy_state_file, log_fn=self.log
        ) as store:
            known = store.load()
            issues = self.jira.search(jcfg.jql)
            now = time.time()
            seeding = jcfg.seed and not known
            comments_on = "comment" in jcfg.events
            budget = jcfg.comment_max_per_poll
            excluded = effective_exclusions(
                jcfg.projects_exclude,
                store.get_setting(MUTED_SETTING, []),
                store.get_setting(UNMUTED_SETTING, []),
            )
            sent = 0

            for issue in issues:
                sent, budget = self._process_issue(
                    issue,
                    known=known,
                    store=store,
                    now=now,
                    seeding=seeding,
                    comments_on=comments_on,
                    budget=budget,
                    dry_run=dry_run,
                    excluded=excluded,
                )

            store.prune(now)

        self._log_summary(len(issues), len(known), sent, seeding, verbose)
        return sent

    def run(
        self, dry_run: bool = False, once: bool = False, verbose: bool = False
    ) -> None:
        stopping = False

        def handle(_signum, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

        if self.commander is not None and not once:
            self.commander.register()

        while not stopping:
            started = time.time()
            try:
                self.poll_once(dry_run, verbose)
            except urllib.error.HTTPError as err:
                body = err.read().decode(errors="replace")[:300]
                self.log(f"jira request failed: HTTP {err.code} {body}")
            except urllib.error.URLError as err:
                self.log(f"jira is unreachable: {err.reason}")
            except (RuntimeError, SystemExit) as err:
                self.log(f"poll failed: {err}")
                if once or not self.cfg.telegram.token:
                    raise

            if once:
                break

            if self.commander is not None:
                self._handle_commands(time.time() - started)

            elapsed = time.time() - started
            time.sleep(max(MIN_INTERVAL, self.cfg.jira.interval - elapsed))

    def _handle_commands(self, elapsed: float) -> None:
        """Long-poll Telegram for /projects, /mute and /unmute."""
        room = max(0, int(self.cfg.jira.interval - elapsed))
        try:
            self.commander.handle(min(COMMAND_TIMEOUT, room))
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:200]
            self.log(f"telegram command poll failed: HTTP {err.code} {body}")
        except (RuntimeError, SystemExit, urllib.error.URLError) as err:
            self.log(f"telegram command poll failed: {err}")

    def _process_issue(
        self,
        issue: dict,
        *,
        known: dict[str, IssueSnapshot],
        store: StateStore,
        now: float,
        seeding: bool,
        comments_on: bool,
        budget: int,
        dry_run: bool,
        excluded: set[str],
    ) -> tuple[int, int]:
        jcfg = self.cfg.jira
        key = issue["key"]
        new = build_snapshot(issue, jcfg.server)
        old = known.get(key)

        if old is not None and old.updated == new.updated and old.status == new.status:
            return 0, budget

        if normalize_project_key(new.project) in excluded:
            self.log(f"{key}: project {new.project} is muted, not notified")
            new.comment_id = old.comment_id if old is not None else None
            store.upsert(key, new)
            known[key] = new
            return 0, budget

        comments = self._fetch_comments(
            key, old, new, seeding=seeding, comments_on=comments_on
        )

        if seeding:
            new.comment_id = max_comment_id(comments) if comments_on else 0
            store.upsert(key, new)
            known[key] = new
            return 0, budget

        sent = self._notify_changes(issue, key, old, new, now, dry_run)

        if comments_on:
            comment_sent, budget = self._notify_comments(
                key, old, new, comments, dry_run, budget
            )
            sent += comment_sent

        store.upsert(key, new)
        known[key] = new
        return sent, budget

    def _fetch_comments(
        self,
        key: str,
        old: IssueSnapshot | None,
        new: IssueSnapshot,
        *,
        seeding: bool,
        comments_on: bool,
    ) -> list[dict]:
        if not comments_on:
            return []
        changed = old is None or old.updated != new.updated
        return self.jira.comment_list(key) if seeding or changed else []

    def _notify_changes(
        self,
        issue: dict,
        key: str,
        old: IssueSnapshot | None,
        new: IssueSnapshot,
        now: float,
        dry_run: bool,
    ) -> int:
        events = detect_events(old, new, self.cfg.jira, now)
        mine = self._my_actions(issue, key, old, events)
        sent = 0
        for event in events:
            if event in mine:
                self.log(f"{key}: {event} change is mine, not notified")
                continue
            self._send(compose(event, key, new, old), key, event, dry_run)
            sent += 1
        return sent

    def _my_actions(
        self,
        issue: dict,
        key: str,
        old: IssueSnapshot | None,
        events: list[str],
    ) -> set[str]:
        """Which of the detected events were caused by the token's own user."""
        if not self.cfg.jira.ignore_self or not events:
            return set()

        me = self.me()
        if not me:
            return set()

        if old is None:
            creator = (issue.get("fields") or {}).get("creator")
            return {"created"} if same_person(creator, me) else set()

        tracked = {event for event in events if event in FIELD_EVENTS}
        if not tracked:
            return set()

        latest: dict[str, tuple[float, dict | None]] = {}
        for history in self.jira.changelog(key):
            stamp = epoch(history.get("created"))
            for item in history.get("items") or []:
                field = item.get("field")
                if field and (field not in latest or stamp >= latest[field][0]):
                    latest[field] = (stamp, history.get("author"))

        return {
            field
            for field in tracked
            if same_person(latest.get(field, (0.0, None))[1], me)
        }

    def _notify_comments(
        self,
        key: str,
        old: IssueSnapshot | None,
        new: IssueSnapshot,
        comments: list[dict],
        dry_run: bool,
        budget: int,
    ) -> tuple[int, int]:
        jcfg = self.cfg.jira
        last_id = old.comment_id if old is not None else None
        me = self.me() if jcfg.ignore_self else {}
        fresh = [
            c
            for c in pick_new_comments(comments, last_id or 0)
            if display_name(c.get("author")) not in jcfg.authors_exclude
            and not (me and same_person(c.get("author"), me))
        ]

        sent = 0
        if last_id is not None:
            allowed = max(0, min(len(fresh), budget))
            for comment in fresh[:allowed]:
                self._send(
                    compose_comment(key, new, comment, jcfg.comment_chars),
                    key,
                    "comment",
                    dry_run,
                )
                sent += 1
            if len(fresh) > allowed:
                self.log(
                    f"{key}: {len(fresh) - allowed} new comment(s) skipped (per-poll cap)"
                )
            budget -= allowed

        new.comment_id = max(last_id or 0, max_comment_id(comments))
        return sent, budget

    def _send(self, text: str, key: str, event: str, dry_run: bool) -> None:
        try:
            self.telegram.send_message(text, dry_run)
        except (RuntimeError, SystemExit) as err:
            self.log(f"failed to notify {key} ({event}): {err}")
            raise

    def _log_summary(self, fetched, known, sent, seeding, verbose):
        if seeding:
            self.log(
                f"first run: seeded {known} issues, nothing sent "
                "(set notify.seed = false to get history)"
            )
        elif sent:
            self.log(f"processed {fetched} issues, sent {sent} message(s)")
        elif verbose:
            self.log(f"processed {fetched} issues, nothing to send")
