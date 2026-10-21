import json
import os
import re
import signal
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jira_tg_notify.commands import Commander  # noqa: E402
from jira_tg_notify.config import (  # noqa: E402
    DEFAULT_TODO_JQL,
    Config,
    JiraConfig,
    LAST_POLL_SETTING,
    OFFSET_SETTING,
    TelegramConfig,
    effective_exclusions,
    load_config,
    parse_interval,
    todo_query,
)
from jira_tg_notify.events import detect_events  # noqa: E402
from jira_tg_notify.formatting import (  # noqa: E402
    clean_markup,
    compose,
    compose_comment,
)
from jira_tg_notify.jira import JiraClient  # noqa: E402
from jira_tg_notify.models import (  # noqa: E402
    IssueSnapshot,
    build_snapshot,
    comment_id,
    epoch,
    pick_new_comments,
)
from jira_tg_notify.poller import Poller  # noqa: E402
from jira_tg_notify.storage import PRUNE_SECONDS, StateStore  # noqa: E402

JIRA_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"
SERVER = "https://jira.example.com"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def make_jira(**overrides) -> JiraConfig:
    base = dict(
        server=SERVER,
        jql="updated >= -10m ORDER BY updated ASC",
        interval=60,
        state_db=tmpdir() / "state.db",
        legacy_state_file=None,
        events={"created", "status", "assignee", "priority"},
        seed=True,
        auth_type="bearer",
        api_token="token",
        login="",
        netrc_host="jira.example.com",
        window_minutes=10,
        comment_chars=400,
        comment_max_per_poll=10,
        authors_exclude=[],
        projects_exclude=[],
        ignore_self=False,
    )
    base.update(overrides)
    return JiraConfig(**base)


def make_config(jira: JiraConfig | None = None) -> Config:
    return Config(
        jira=jira or make_jira(),
        telegram=TelegramConfig(
            token="",
            chat_id="-100",
            thread_id=None,
            api_base="https://api.telegram.org",
        ),
    )


def to_jira(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(JIRA_FMT)


def make_issue(
    key="TEST-1",
    status="На проверке",
    assignee="Иван",
    priority="High",
    updated=None,
    created=None,
    project="TEST",
    creator="Пётр",
    creator_name="petr",
    reporter=None,
    reporter_name=None,
    watcher=False,
):
    now = time.time()

    return {
        "key": key,
        "fields": {
            "summary": "Проверить <баннер> & размеры",
            "status": {"name": status},
            "assignee": {"displayName": assignee} if assignee else None,
            "priority": {"name": priority},
            "project": {"key": project},
            "creator": {"displayName": creator, "name": creator_name},
            "reporter": (
                {"displayName": reporter, "name": reporter_name or reporter.lower()}
                if reporter
                else None
            ),
            "watches": {"watchCount": int(bool(watcher)), "isWatching": bool(watcher)},
            "created": to_jira(created if created is not None else now - 3600),
            "updated": to_jira(updated if updated is not None else now),
        },
    }


def make_comment(cid, author="Пётр", body="Текст комментария", name=None, created=None):
    return {
        "id": str(cid),
        "author": {"displayName": author, "name": name or author.lower()},
        "body": body,
        "created": to_jira(created if created is not None else time.time()),
    }


def make_history(field="status", author="Пётр", name="petr", created=None):
    return {
        "created": to_jira(created if created is not None else time.time()),
        "author": {"displayName": author, "name": name},
        "items": [{"field": field}],
    }


class FakeJira:
    def __init__(self):
        self.issues: list[dict] = []
        self.comments: dict[str, list[dict]] = {}
        self.comment_calls: list[str] = []
        self.histories: dict[str, list[dict]] = {}
        self.changelog_calls: list[str] = []
        self.jqls: list[str] = []
        self.me = {"name": "igor", "key": "JIRAUSER1", "displayName": "Игорь"}

    def search(self, jql=None):
        self.jqls.append(jql or "")
        return self.issues

    def comment_list(self, key):
        self.comment_calls.append(key)
        return self.comments.get(key, [])

    def myself(self):
        return self.me

    def changelog(self, key):
        self.changelog_calls.append(key)
        return self.histories.get(key, [])


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text, dry_run=False):
        self.sent.append(text)


class FakeBot(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.updates: list[dict] = []
        self.offsets: list[int | None] = []
        self.commands = None

    def get_updates(self, offset=None, timeout=0):
        self.offsets.append(offset)
        batch, self.updates = self.updates, []
        return batch

    def set_commands(self, commands):
        self.commands = commands


def bot_message(text, chat_id=-100500, update_id=1):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


class PollerTestCase(unittest.TestCase):
    def setUp(self):
        self.jira = make_jira()
        self.config = make_config(self.jira)
        self.feed = FakeJira()
        self.telegram = FakeTelegram()
        self.poller = Poller(
            self.config,
            jira=self.feed,
            telegram=self.telegram,
            log_fn=lambda _msg: None,
        )

    def stored(self) -> dict[str, IssueSnapshot]:
        with StateStore(self.jira.state_db) as store:
            return store.load()


class TestTime(unittest.TestCase):
    def test_epoch(self):
        expected = datetime(2026, 10, 20, 7, 26, 46, tzinfo=timezone.utc).timestamp()
        self.assertEqual(expected, epoch("2026-10-20T10:26:46.000+0300"))

    def test_epoch_bad(self):
        self.assertEqual(0.0, epoch("junk"))
        self.assertEqual(0.0, epoch(None))


class TestCompose(unittest.TestCase):
    def test_created_escapes_html(self):
        snap = build_snapshot(make_issue(), SERVER)
        text = compose("created", "TEST-1", snap, None)

        self.assertIn("TEST-1", text)
        self.assertIn("Проверить &lt;баннер&gt; &amp; размеры", text)
        self.assertIn("https://jira.example.com/browse/TEST-1", text)

    def test_status_arrow(self):
        old = build_snapshot(make_issue(status="На проверке"), SERVER)
        new = build_snapshot(make_issue(status="Готово"), SERVER)
        text = compose("status", "TEST-1", new, old)

        self.assertIn("На проверке → <b>Готово</b>", text)

    def test_priority_and_assignee(self):
        old = build_snapshot(make_issue(assignee="Иван", priority="High"), SERVER)
        new = build_snapshot(make_issue(assignee="Пётр", priority="Low"), SERVER)

        self.assertIn("Иван → <b>Пётр</b>", compose("assignee", "T-1", new, old))
        self.assertIn("High → <b>Low</b>", compose("priority", "T-1", new, old))


class TestDetectEvents(unittest.TestCase):
    def test_new_issue_in_window(self):
        jira = make_jira()
        snap = build_snapshot(make_issue(created=time.time() - 60), SERVER)
        self.assertEqual(["created"], detect_events(None, snap, jira, time.time()))

    def test_new_issue_outside_window(self):
        jira = make_jira()
        snap = build_snapshot(make_issue(created=time.time() - 86400), SERVER)
        self.assertEqual([], detect_events(None, snap, jira, time.time()))

    def test_field_changes(self):
        jira = make_jira()
        old = build_snapshot(
            make_issue(status="A", assignee="Иван", priority="High"), SERVER
        )
        new = build_snapshot(
            make_issue(status="B", assignee="Пётр", priority="Low"), SERVER
        )
        self.assertEqual(
            ["status", "assignee", "priority"],
            detect_events(old, new, jira, time.time()),
        )

    def test_events_filter(self):
        jira = make_jira(events={"status"})
        old = build_snapshot(make_issue(status="A", assignee="Иван"), SERVER)
        new = build_snapshot(make_issue(status="B", assignee="Пётр"), SERVER)
        self.assertEqual(["status"], detect_events(old, new, jira, time.time()))

    def test_no_changes(self):
        jira = make_jira()
        snap = build_snapshot(make_issue(), SERVER)
        self.assertEqual([], detect_events(snap, snap, jira, time.time()))


class TestSearch(unittest.TestCase):
    def test_pagination(self):
        pages = []

        def fake_fetch(url, headers):
            pages.append(url)
            start = int(url.split("startAt=")[1].split("&")[0])
            batch = [{"key": f"TEST-{i}"} for i in range(start, min(start + 100, 250))]
            return {"issues": batch, "total": 250}

        issues = JiraClient(make_jira(), fetch=fake_fetch).search("jql")

        self.assertEqual(250, len(issues))
        self.assertEqual(3, len(pages))


class TestStorage(unittest.TestCase):
    def test_roundtrip_and_prune(self):
        path = tmpdir() / "nested" / "state.db"
        now = time.time()

        with StateStore(path) as store:
            store.upsert("OLD-1", IssueSnapshot(updated=now - PRUNE_SECONDS - 10))
            store.upsert("NEW-1", IssueSnapshot(updated=now, status="Готово"))
            store.prune(now)
            loaded = store.load()

        self.assertNotIn("OLD-1", loaded)
        self.assertEqual("Готово", loaded["NEW-1"].status)

    def test_upsert_updates_existing_row(self):
        path = tmpdir() / "state.db"
        with StateStore(path) as store:
            store.upsert("TEST-1", IssueSnapshot(updated=1, status="A"))
            store.upsert("TEST-1", IssueSnapshot(updated=2, status="B", comment_id=7))
            loaded = store.load()

        self.assertEqual(1, len(loaded))
        self.assertEqual("B", loaded["TEST-1"].status)
        self.assertEqual(7, loaded["TEST-1"].comment_id)

    def test_legacy_json_is_imported_once(self):
        folder = tmpdir()
        legacy = folder / "state.json"
        legacy.write_text(
            '{"version": 1, "issues": {"TEST-1": {"updated": 123, "status": "Done",'
            ' "comment_id": 5, "url": "https://x/browse/TEST-1"}}}'
        )

        with StateStore(folder / "state.db", legacy) as store:
            loaded = store.load()

        self.assertEqual("Done", loaded["TEST-1"].status)
        self.assertEqual(5, loaded["TEST-1"].comment_id)
        self.assertFalse(legacy.exists())
        self.assertTrue((folder / "state.json.migrated").exists())

    def test_unreadable_legacy_json_is_quarantined(self):
        folder = tmpdir()
        legacy = folder / "state.json"
        legacy.write_text("{not json")

        with StateStore(folder / "state.db", legacy) as store:
            self.assertEqual({}, store.load())

        self.assertTrue((folder / "state.json.invalid").exists())


class TestPoll(PollerTestCase):
    def test_seed_sends_nothing_then_events(self):
        self.feed.issues = [make_issue(key="TEST-1", status="Backlog")]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertIn("TEST-1", self.stored())

        self.feed.issues = [make_issue(key="TEST-1", status="Готово")]
        self.assertEqual(1, self.poller.poll_once(dry_run=True))

        self.feed.issues = [make_issue(key="TEST-2", created=time.time() - 60)]
        self.assertEqual(1, self.poller.poll_once(dry_run=True))

    def test_unchanged_issue_is_skipped(self):
        self.feed.issues = [make_issue(key="TEST-1")]
        self.poller.poll_once(dry_run=True)
        self.assertEqual(0, self.poller.poll_once(dry_run=True))

    def test_state_survives_restart(self):
        self.feed.issues = [make_issue(key="TEST-1", status="Backlog")]
        self.poller.poll_once(dry_run=True)

        restarted = Poller(
            self.config,
            jira=self.feed,
            telegram=self.telegram,
            log_fn=lambda _msg: None,
        )
        self.feed.issues = [make_issue(key="TEST-1", status="Backlog")]
        self.assertEqual(0, restarted.poll_once(dry_run=True))

    def test_legacy_state_import_does_not_reseed(self):
        issue = make_issue(key="TEST-1", status="Backlog")
        legacy = self.jira.state_db.with_suffix(".json")
        legacy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "issues": {"TEST-1": asdict(build_snapshot(issue, SERVER))},
                }
            )
        )
        self.jira.legacy_state_file = legacy
        self.feed.issues = [issue]

        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertFalse(legacy.exists())


class TestCatchup(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.jira.events = {"created", "status", "assignee", "priority", "comment"}

    def rewind(self, seconds):
        with StateStore(self.jira.state_db) as store:
            store.set_setting(LAST_POLL_SETTING, time.time() - seconds)

    def lookback(self):
        match = re.search(r"updated >= -(\d+)m", self.feed.jqls[-1])
        self.assertIsNotNone(match, self.feed.jqls[-1])
        return int(match.group(1))

    def test_no_gap_keeps_the_configured_jql(self):
        self.poller.poll_once(dry_run=True)
        self.assertEqual("updated >= -10m ORDER BY updated ASC", self.feed.jqls[-1])

    def test_downtime_widens_the_window(self):
        self.poller.poll_once(dry_run=True)
        self.rewind(2 * 3600)

        self.poller.poll_once(dry_run=True)

        self.assertGreaterEqual(self.lookback(), 120)
        self.assertLessEqual(self.lookback(), 135)
        self.assertTrue(self.feed.jqls[-1].endswith("ORDER BY updated DESC"))

    def test_long_downtime_is_capped(self):
        self.jira.catchup_hours = 6
        self.poller.poll_once(dry_run=True)
        self.rewind(3 * 24 * 3600)

        self.poller.poll_once(dry_run=True)

        self.assertEqual(6 * 60, self.lookback())

    def test_catchup_can_be_disabled(self):
        self.jira.catchup_hours = 0
        self.poller.poll_once(dry_run=True)
        self.rewind(2 * 3600)

        self.poller.poll_once(dry_run=True)

        self.assertEqual("updated >= -10m ORDER BY updated ASC", self.feed.jqls[-1])

    def test_catchup_needs_a_relative_window(self):
        self.jira.jql = "project = TEST ORDER BY updated ASC"
        self.poller.poll_once(dry_run=True)
        self.rewind(2 * 3600)

        self.poller.poll_once(dry_run=True)

        self.assertEqual("project = TEST ORDER BY updated ASC", self.feed.jqls[-1])

    def test_comment_from_the_gap_is_delivered(self):
        self.feed.issues = [make_issue(key="TEST-0", status="Backlog")]
        self.poller.poll_once(dry_run=True)
        self.rewind(3 * 3600)

        comment = make_comment(11719, author="Мария", created=time.time() - 2 * 3600)
        self.feed.issues = [
            make_issue(
                key="VPSH-78",
                created=time.time() - 5 * 86400,
                updated=time.time() - 2 * 3600,
            )
        ]
        self.feed.comments = {"VPSH-78": [comment]}

        self.assertEqual(1, self.poller.poll_once(dry_run=True))
        self.assertEqual(11719, self.stored()["VPSH-78"].comment_id)

    def test_comment_older_than_the_gap_is_not_replayed(self):
        self.feed.issues = [make_issue(key="TEST-0", status="Backlog")]
        self.poller.poll_once(dry_run=True)
        self.rewind(3 * 3600)

        comment = make_comment(1, created=time.time() - 5 * 86400)
        self.feed.issues = [make_issue(key="OLD-1", created=time.time() - 9 * 86400)]
        self.feed.comments = {"OLD-1": [comment]}

        self.assertEqual(0, self.poller.poll_once(dry_run=True))


class TestResilience(unittest.TestCase):
    def test_command_timeout_is_logged_not_raised(self):
        logs = []

        class Boom:
            def handle(self, timeout=0):
                raise TimeoutError("The read operation timed out")

        poller = Poller(
            make_config(),
            jira=FakeJira(),
            telegram=FakeTelegram(),
            log_fn=logs.append,
            commander=Boom(),
        )

        poller._handle_commands(1.0)

        self.assertTrue(any("timed out" in line for line in logs))

    def test_daemon_survives_a_socket_timeout(self):
        logs = []

        class FlakyJira(FakeJira):
            calls = 0

            def search(self, jql=None):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("The read operation timed out")
                os.kill(os.getpid(), signal.SIGTERM)
                return []

        jira = FlakyJira()
        poller = Poller(
            make_config(),
            jira=jira,
            telegram=FakeTelegram(),
            log_fn=logs.append,
        )

        with mock.patch("jira_tg_notify.poller.time.sleep", lambda _s: None):
            poller.run()

        self.assertEqual(2, jira.calls)
        self.assertTrue(any("timed out" in line for line in logs))


class TestComments(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.jira.events = {"created", "status", "assignee", "priority", "comment"}

    def poll(self, comments, status="На проверке", key="TEST-1", updated=None):
        self.feed.issues = [make_issue(key=key, status=status, updated=updated)]
        self.feed.comments = {key: comments}
        return self.poller.poll_once(dry_run=True)

    def test_seed_records_last_comment_without_notifying(self):
        self.assertEqual(0, self.poll([make_comment(10)]))
        self.assertEqual(10, self.stored()["TEST-1"].comment_id)

    def test_new_comment_notifies_once(self):
        self.poll([make_comment(10)])
        self.assertEqual(1, self.poll([make_comment(11, author="Анна")]))
        self.assertEqual(0, self.poll([make_comment(11, author="Анна")]))

    def test_excluded_author_is_skipped_but_remembered(self):
        self.poll([make_comment(10)])

        self.jira.authors_exclude = ["integration-bot"]
        self.assertEqual(0, self.poll([make_comment(11, author="integration-bot")]))
        self.assertEqual(1, self.poll([make_comment(12, author="Анна")]))

    def test_many_comments_are_capped(self):
        self.jira.comment_max_per_poll = 1
        self.poll([make_comment(1)])

        comments = [make_comment(cid) for cid in range(2, 6)]
        self.assertEqual(1, self.poll(comments))
        self.assertEqual(5, self.stored()["TEST-1"].comment_id)

    def test_comments_not_fetched_for_unchanged_issue(self):
        updated = time.time()
        self.poll([make_comment(10)], updated=updated)
        self.feed.comment_calls.clear()
        self.assertEqual(0, self.poll([make_comment(10)], updated=updated))
        self.assertEqual([], self.feed.comment_calls)

    def test_clean_markup(self):
        raw = (
            "h2. Заголовок\n"
            "[https://example.com|ссылка] и [https://example.com]\n"
            "{code:java}int x = 1;{code}\n"
            "*жирный* _курсив_ {{код}} !image.png!\n"
            "<br>конец"
        )
        cleaned = clean_markup(raw)

        self.assertIn("Заголовок", cleaned)
        self.assertIn("ссылка (https://example.com)", cleaned)
        self.assertIn("int x = 1;", cleaned)
        self.assertIn("жирный курсив код image.png", cleaned)
        self.assertIn("конец", cleaned)
        self.assertNotIn("{code", cleaned)
        self.assertNotIn("<br>", cleaned)

    def test_pick_new_comments(self):
        comments = [make_comment(5), make_comment(7), make_comment(6)]
        fresh = pick_new_comments(comments, 5)
        self.assertEqual([6, 7], [comment_id(c) for c in fresh])

    def test_compose_comment(self):
        snap = build_snapshot(make_issue(), SERVER)
        text = compose_comment(
            "TEST-1", snap, make_comment(42, author="Пётр <b>"), max_chars=400
        )

        self.assertIn("Пётр &lt;b&gt;", text)
        self.assertIn("focusedCommentId=42", text)
        self.assertIn("Текст комментария", text)

    def test_compose_comment_truncates(self):
        snap = build_snapshot(make_issue(), SERVER)
        text = compose_comment(
            "TEST-1", snap, make_comment(1, body="я" * 100), max_chars=20
        )

        self.assertIn("…", text)
        self.assertNotIn("я" * 30, text)


class TestProjectsExclude(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.jira.events = {"created", "status", "assignee", "priority", "comment"}

    def test_change_is_silent_but_remembered(self):
        self.jira.projects_exclude = ["ops"]
        self.feed.issues = [make_issue(key="OPS-1", project="OPS", status="A")]
        self.poller.poll_once(dry_run=True)

        self.feed.issues = [make_issue(key="OPS-1", project="OPS", status="B")]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertEqual("B", self.stored()["OPS-1"].status)

    def test_other_projects_still_notify(self):
        self.jira.projects_exclude = ["OPS"]
        self.feed.issues = [
            make_issue(key="OPS-1", project="OPS", status="A"),
            make_issue(key="TEST-1", project="TEST", status="A"),
        ]
        self.poller.poll_once(dry_run=True)

        self.feed.issues = [
            make_issue(key="OPS-1", project="OPS", status="B"),
            make_issue(key="TEST-1", project="TEST", status="B"),
        ]
        self.assertEqual(1, self.poller.poll_once(dry_run=True))

    def test_new_issue_is_silent(self):
        self.jira.projects_exclude = ["OPS"]
        self.feed.issues = [make_issue(key="TEST-1", status="A")]
        self.poller.poll_once(dry_run=True)

        self.feed.issues = [
            make_issue(key="TEST-1", status="A"),
            make_issue(key="OPS-1", project="OPS", created=time.time() - 60),
        ]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertIn("OPS-1", self.stored())

    def test_comments_are_not_requested(self):
        self.feed.issues = [make_issue(key="OPS-1", project="OPS")]
        self.feed.comments = {"OPS-1": [make_comment(10)]}
        self.poller.poll_once(dry_run=True)
        self.assertEqual(10, self.stored()["OPS-1"].comment_id)

        self.jira.projects_exclude = ["OPS"]
        self.feed.comment_calls.clear()
        self.feed.issues = [make_issue(key="OPS-1", project="OPS", status="B")]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertEqual([], self.feed.comment_calls)
        self.assertEqual(10, self.stored()["OPS-1"].comment_id)

    def test_taking_project_off_the_list_does_not_replay(self):
        self.feed.issues = [make_issue(key="OPS-1", project="OPS", status="A")]
        self.poller.poll_once(dry_run=True)

        self.jira.projects_exclude = ["OPS"]
        self.feed.issues = [make_issue(key="OPS-1", project="OPS", status="B")]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))

        self.jira.projects_exclude = []
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertEqual([], self.telegram.sent)


class TestIgnoreSelf(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.jira.ignore_self = True
        self.jira.events = {"created", "status", "assignee", "priority", "comment"}
        self.feed.issues = [make_issue(key="TEST-1", status="A")]
        self.feed.comments = {"TEST-1": [make_comment(10)]}
        self.assertEqual(0, self.poller.poll_once(dry_run=True))

    def poll(self, status="B", **issue_kwargs):
        self.feed.issues = [make_issue(key="TEST-1", status=status, **issue_kwargs)]
        return self.poller.poll_once(dry_run=True)

    def test_own_status_change_is_silent(self):
        self.feed.histories["TEST-1"] = [
            make_history("status", author="Игорь", name="igor")
        ]
        self.assertEqual(0, self.poll())
        self.assertEqual("B", self.stored()["TEST-1"].status)

    def test_foreign_status_change_notifies(self):
        self.feed.histories["TEST-1"] = [make_history("status")]
        self.assertEqual(1, self.poll())

    def test_only_own_fields_are_muted(self):
        self.feed.histories["TEST-1"] = [
            make_history("status", author="Игорь", name="igor"),
            make_history("priority"),
        ]
        self.assertEqual(1, self.poll(priority="Low"))

    def test_latest_change_wins(self):
        now = time.time()
        self.feed.histories["TEST-1"] = [
            make_history("status", author="Игорь", name="igor", created=now - 30),
            make_history("status", author="Пётр", name="petr", created=now),
        ]
        self.assertEqual(1, self.poll())

    def test_own_comment_is_silent_but_remembered(self):
        self.feed.comments = {
            "TEST-1": [make_comment(10), make_comment(11, author="Игорь", name="igor")]
        }
        self.assertEqual(0, self.poll(status="A"))
        self.assertEqual(11, self.stored()["TEST-1"].comment_id)

    def test_comment_by_colleague_notifies(self):
        self.feed.comments = {
            "TEST-1": [make_comment(10), make_comment(11, author="Анна")]
        }
        self.assertEqual(1, self.poll(status="A"))

    def test_own_issue_creation_is_silent(self):
        self.feed.issues = [
            make_issue(key="TEST-1", status="A"),
            make_issue(
                key="TEST-2",
                created=time.time() - 60,
                creator="Игорь",
                creator_name="igor",
            ),
        ]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertIn("TEST-2", self.stored())

    def test_issue_created_by_colleague_notifies(self):
        self.feed.issues = [
            make_issue(key="TEST-1", status="A"),
            make_issue(key="TEST-2", created=time.time() - 60),
        ]
        self.assertEqual(1, self.poller.poll_once(dry_run=True))

    def test_off_by_default(self):
        self.jira.ignore_self = False
        self.feed.histories["TEST-1"] = [
            make_history("status", author="Игорь", name="igor")
        ]

        self.assertEqual(1, self.poll())
        self.assertEqual([], self.feed.changelog_calls)


class TestInvolvement(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.jira.only_involved = True
        self.jira.events = {"created", "status", "assignee", "priority", "comment"}

    def seed(self, **issue_kwargs):
        self.feed.issues = [make_issue(key="TEST-1", status="A", **issue_kwargs)]
        return self.poller.poll_once(dry_run=True)

    def change(self, **issue_kwargs):
        self.feed.issues = [make_issue(key="TEST-1", status="B", **issue_kwargs)]
        return self.poller.poll_once(dry_run=True)

    def test_unrelated_issue_is_silent_and_remembered(self):
        self.seed()
        self.assertEqual(0, self.change())
        self.assertEqual("B", self.stored()["TEST-1"].status)

    def test_assignee_is_notified(self):
        self.seed(assignee="Игорь")
        self.assertEqual(1, self.change(assignee="Игорь"))

    def test_reporter_is_notified(self):
        self.seed(reporter="Игорь", reporter_name="igor")
        self.assertEqual(1, self.change(reporter="Игорь", reporter_name="igor"))

    def test_creator_is_notified(self):
        self.seed(creator="Игорь", creator_name="igor")
        self.assertEqual(1, self.change(creator="Игорь", creator_name="igor"))

    def test_watcher_is_notified(self):
        self.seed(watcher=True)
        self.assertEqual(1, self.change(watcher=True))

    def test_comment_author_is_notified(self):
        self.feed.comments = {"TEST-1": [make_comment(10, author="Игорь", name="igor")]}
        self.seed()
        self.assertEqual(1, self.change())

    def test_mentioned_user_is_notified(self):
        self.feed.comments = {
            "TEST-1": [make_comment(10, author="Анна", body="Глянь [~igor], плиз")]
        }
        self.seed()
        self.assertEqual(1, self.change())

    def test_mention_of_someone_else_stays_silent(self):
        self.seed()
        self.feed.comments = {
            "TEST-1": [make_comment(10, author="Анна", body="Глянь [~petr]")]
        }
        self.assertEqual(0, self.change())

    def test_unknown_user_notifies_everything(self):
        self.feed.me = {}
        self.seed()
        self.assertEqual(1, self.change())

    def test_off_by_default(self):
        self.jira.only_involved = False
        self.seed()
        self.assertEqual(1, self.change())

    def test_involvement_works_without_comment_events(self):
        self.jira.events = {"status"}
        self.seed()
        self.feed.comments = {"TEST-1": [make_comment(10, author="Игорь", name="igor")]}

        self.assertEqual(1, self.change())
        self.assertIn("TEST-1", self.feed.comment_calls)


class TestCommander(unittest.TestCase):
    def setUp(self):
        self.jira = make_jira(projects_exclude=["HSNS"])
        self.cfg = make_config(self.jira)
        self.cfg.telegram.chat_id = "-100500"
        self.bot = FakeBot()
        self.feed = FakeJira()
        self.commander = Commander(
            self.cfg,
            telegram=self.bot,
            log_fn=lambda _msg: None,
            jira=self.feed,
        )

    def send(self, text, chat_id=-100500, update_id=1):
        self.bot.updates = [bot_message(text, chat_id, update_id)]
        self.commander.handle()
        return self.bot.sent[-1] if self.bot.sent else ""

    def test_todo_lists_assigned_issues(self):
        self.feed.issues = [
            make_issue(key="VPSH-78", project="VPSH", status="На тестировании")
        ]

        text = self.send("/todo")

        self.assertIn("VPSH-78", text)
        self.assertIn("На тестировании", text)
        self.assertIn("browse/VPSH-78", text)

    def test_todo_without_issues(self):
        self.feed.issues = []
        self.assertIn("нет", self.send("/todo"))

    def test_todo_caps_the_list(self):
        self.jira.todo_limit = 2
        self.feed.issues = [make_issue(key=f"TEST-{n}") for n in range(5)]

        text = self.send("/todo")

        self.assertIn("TEST-0", text)
        self.assertIn("TEST-1", text)
        self.assertNotIn("TEST-2", text)
        self.assertIn("…и ещё 3", text)

    def test_todo_aliases(self):
        self.feed.issues = []
        self.assertIn("нет", self.send("/mine"))
        self.assertIn("нет", self.send("/tasks"))

    def test_todo_applies_excluded_statuses(self):
        self.jira.todo_exclude_statuses = ["На ревью"]

        self.feed.issues = []
        self.send("/todo")

        self.assertIn('status NOT IN ("На ревью")', self.feed.jqls[-1])
        self.assertIn("ORDER BY updated DESC", self.feed.jqls[-1])

    def test_todo_reports_a_jira_failure(self):
        class Broken(FakeJira):
            def search(self, jql=None):
                raise OSError("boom")

        self.commander._jira = Broken()

        self.assertIn("Не получилось", self.send("/todo"))

    def mute_list(self):
        return self.send("/projects")

    def test_projects_lists_config_keys(self):
        self.assertIn("HSNS", self.mute_list())

    def test_mute_adds_projects(self):
        self.assertIn("Заглушил", self.send("/mute OPS tosh"))
        self.assertIn("OPS", self.mute_list())
        self.assertIn("TOSH", self.mute_list())

    def test_unmute_removes_a_config_key(self):
        self.send("/unmute hsns")
        self.assertNotIn("HSNS", self.mute_list())

    def test_mute_without_arguments_hints(self):
        self.assertIn("/mute OPS", self.send("/mute"))

    def test_invalid_key_is_rejected(self):
        self.assertIn("Не похоже", self.send("/mute OPS-1"))

    def test_other_chats_are_ignored(self):
        self.assertEqual("", self.send("/mute OPS", chat_id=42))

    def test_plain_messages_are_ignored(self):
        self.assertEqual("", self.send("привет"))

    def test_unknown_command_shows_help(self):
        self.assertIn("/projects", self.send("/start"))

    def test_offset_survives_restarts(self):
        self.send("/projects", update_id=41)
        with StateStore(self.jira.state_db) as store:
            self.assertEqual(42, store.get_setting(OFFSET_SETTING))

        self.commander.handle()
        self.assertEqual(42, self.bot.offsets[-1])

    def test_mute_and_unmute_drive_the_poller(self):
        feed = FakeJira()
        sent = FakeTelegram()
        poller = Poller(
            self.cfg,
            jira=feed,
            telegram=sent,
            log_fn=lambda _msg: None,
            commander=self.commander,
        )

        feed.issues = [make_issue(project="TEST", status="A")]
        self.assertEqual(0, poller.poll_once(dry_run=True))

        self.send("/mute TEST")
        feed.issues = [make_issue(project="TEST", status="B")]
        self.assertEqual(0, poller.poll_once(dry_run=True))

        self.send("/unmute TEST")
        feed.issues = [make_issue(project="TEST", status="C")]
        self.assertEqual(1, poller.poll_once(dry_run=True))


class TestConfig(unittest.TestCase):
    def test_load_defaults(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[telegram]
token = "123:abc"
chat_id = "-100500"

[jira]
server = "https://jira.example.com/"

[notify]
interval = "2m"
state_file = "~/state/state.json"
"""
        )

        cfg = load_config(path)

        self.assertEqual("https://jira.example.com", cfg.jira.server)
        self.assertEqual(120, cfg.jira.interval)
        self.assertEqual("updated >= -10m ORDER BY updated ASC", cfg.jira.jql)
        self.assertTrue(cfg.jira.seed)
        self.assertEqual(24, cfg.jira.catchup_hours)
        self.assertEqual([], cfg.jira.todo_exclude_statuses)
        self.assertEqual("-100500", cfg.telegram.chat_id)
        self.assertIsNone(cfg.telegram.thread_id)
        self.assertEqual("state.db", cfg.jira.state_db.name)
        self.assertEqual("state.json", cfg.jira.legacy_state_file.name)

    def test_state_db_key_wins(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[jira]
server = "https://jira.example.com"

[notify]
state_db = "~/new/state.db"
"""
        )

        cfg = load_config(path)
        self.assertEqual("state.db", cfg.jira.state_db.name)
        self.assertEqual("new", cfg.jira.state_db.parent.name)

    def test_parse_interval(self):
        self.assertEqual(30, parse_interval(30))
        self.assertEqual(30, parse_interval("30s"))
        self.assertEqual(3600, parse_interval("1h"))

    def test_projects_exclude_is_case_insensitive(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[jira]
server = "https://jira.example.com"

[notify]
projects_exclude = ["OPS", "support"]
"""
        )

        cfg = load_config(path)
        excluded = effective_exclusions(cfg.jira.projects_exclude)
        self.assertIn("OPS", excluded)
        self.assertIn("SUPPORT", excluded)
        self.assertNotIn("TEST", excluded)
        self.assertEqual({"OPS", "SUPPORT"}, excluded)

    def test_ignore_self(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[jira]
server = "https://jira.example.com"

[notify]
ignore_self = true
"""
        )
        self.assertTrue(load_config(path).jira.ignore_self)

    def test_todo_exclude_statuses(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[jira]
server = "https://jira.example.com"

[notify]
todo_exclude_statuses = ["На ревью", "On review"]
"""
        )
        self.assertEqual(
            ["На ревью", "On review"],
            load_config(path).jira.todo_exclude_statuses,
        )

    def test_todo_query_without_exclusions(self):
        self.assertEqual(DEFAULT_TODO_JQL, todo_query(make_jira()))

    def test_todo_query_appends_before_order_by(self):
        jira = make_jira(todo_exclude_statuses=["На ревью", "На тестировании"])
        query = todo_query(jira)

        self.assertTrue(query.startswith(DEFAULT_TODO_JQL.split(" ORDER BY")[0]))
        self.assertIn('AND status NOT IN ("На ревью", "На тестировании")', query)
        self.assertTrue(query.endswith("ORDER BY updated DESC"))

    def test_todo_query_wraps_a_query_without_order_by(self):
        jira = make_jira(todo_jql="project = A OR project = B", todo_exclude_statuses=["X"])
        self.assertEqual(
            '(project = A OR project = B) AND status NOT IN ("X")', todo_query(jira)
        )

    def test_todo_query_escapes_quotes(self):
        jira = make_jira(todo_exclude_statuses=['He said "no"'])
        self.assertIn('status NOT IN ("He said \\"no\\"")', todo_query(jira))

    def test_only_involved(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[jira]
server = "https://jira.example.com"

[notify]
only_involved = true
"""
        )
        self.assertTrue(load_config(path).jira.only_involved)


if __name__ == "__main__":
    unittest.main()
