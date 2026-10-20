#!/usr/bin/env python3
"""Jira -> Telegram notifier: polls Jira by JQL and posts issue events to a Telegram chat."""

from __future__ import annotations

import argparse
import base64
import html
import json
import netrc
import os
import re
import signal
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FIELDS = "summary,status,assignee,priority,project,issuetype,created,updated"
PAGE_SIZE = 100
MAX_RESULTS = 1000
TELEGRAM_LIMIT = 4096
STATE_VERSION = 1
PRUNE_SECONDS = 30 * 24 * 3600

JIRA_DATETIME = "%Y-%m-%dT%H:%M:%S.%f%z"

EVENT_ICONS = {
    "created": "🆕",
    "status": "🔄",
    "assignee": "👤",
    "priority": "⚡",
    "comment": "💬",
}

COMMENT_PAGE = 100


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {msg}", file=sys.stderr, flush=True)


@dataclass
class JiraConfig:
    server: str
    jql: str
    interval: int
    state_file: Path
    events: set[str]
    seed: bool
    auth_type: str
    api_token: str
    login: str
    netrc_host: str
    window_minutes: int
    comment_chars: int
    comment_max_per_poll: int
    authors_exclude: list[str]


@dataclass
class TelegramConfig:
    token: str
    chat_id: str
    thread_id: int | None
    api_base: str


@dataclass
class Config:
    jira: JiraConfig
    telegram: TelegramConfig


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def parse_interval(value) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1:] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    return int(text)


def load_config(path: Path) -> Config:
    if not path.exists():
        raise SystemExit(
            f"config not found: {path}\ncopy config.example.toml and fill it in"
        )

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    tg = raw.get("telegram", {})
    jr = raw.get("jira", {})
    nt = raw.get("notify", {})

    server = (os.environ.get("JIRA_SERVER") or jr.get("server") or "").rstrip("/")
    if not server:
        raise SystemExit("jira.server is not set")

    default_jql = (
        f"updated >= -{int(nt.get('window_minutes', 10))}m ORDER BY updated ASC"
    )

    jira = JiraConfig(
        server=server,
        jql=nt.get("jql") or default_jql,
        interval=parse_interval(nt.get("interval", 60)),
        state_file=expand(
            nt.get("state_file", "~/.local/state/jira-tg-notify/state.json")
        ),
        events=set(
            nt.get("events", ["created", "status", "assignee", "priority", "comment"])
        ),
        seed=bool(nt.get("seed", True)),
        auth_type=(jr.get("auth_type") or "bearer").lower(),
        api_token=os.environ.get("JIRA_TOKEN") or jr.get("api_token") or "",
        login=jr.get("login") or "",
        netrc_host=jr.get("netrc_host") or urllib.parse.urlparse(server).hostname or "",
        window_minutes=int(nt.get("window_minutes", 10)),
        comment_chars=int(nt.get("comment_chars", 400)),
        comment_max_per_poll=int(nt.get("comment_max_per_poll", 10)),
        authors_exclude=list(nt.get("authors_exclude", [])),
    )

    telegram = TelegramConfig(
        token=os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("token") or "",
        chat_id=str(os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id") or ""),
        thread_id=tg.get("message_thread_id") or None,
        api_base=(tg.get("api_base") or "https://api.telegram.org").rstrip("/"),
    )

    return Config(jira=jira, telegram=telegram)


def netrc_password(host: str) -> tuple[str, str]:
    try:
        auth = netrc.netrc().authenticators(host)
    except (FileNotFoundError, netrc.NetrcParseError):
        return "", ""
    if not auth:
        return "", ""
    login, _, password = auth
    return login or "", password or ""


def jira_headers(cfg: JiraConfig) -> dict[str, str]:
    token = cfg.api_token
    login = cfg.login

    if not token:
        login, token = netrc_password(cfg.netrc_host)
    if not token:
        raise SystemExit(
            f"no Jira token: set jira.api_token, JIRA_TOKEN or ~/.netrc entry for {cfg.netrc_host}"
        )

    if cfg.auth_type == "basic":
        raw = base64.b64encode(f"{login}:{token}".encode()).decode()
        value = f"Basic {raw}"
    else:
        value = f"Bearer {token}"

    return {"Authorization": value, "Accept": "application/json"}


def http_json(url: str, headers: dict[str, str], timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def jira_search(cfg: JiraConfig, headers: dict[str, str], jql: str) -> list[dict]:
    issues: list[dict] = []
    start = 0

    while start < MAX_RESULTS:
        params = urllib.parse.urlencode(
            {
                "jql": jql,
                "startAt": start,
                "maxResults": PAGE_SIZE,
                "fields": FIELDS,
                "validateQuery": "false",
            }
        )
        data = http_json(f"{cfg.server}/rest/api/2/search?{params}", headers)

        batch = data.get("issues", [])
        issues.extend(batch)

        total = int(data.get("total", 0))
        start += len(batch)
        if not batch or start >= total:
            break

    return issues


def epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.strptime(value, JIRA_DATETIME).timestamp()
    except ValueError:
        return 0.0


def display_name(person: dict | None) -> str:
    if not person:
        return "—"
    return person.get("displayName") or person.get("name") or "—"


def snapshot(issue: dict, jira: JiraConfig) -> dict:
    f = issue["fields"]
    key = issue["key"]

    return {
        "updated": epoch(f.get("updated")),
        "created": epoch(f.get("created")),
        "status": (f.get("status") or {}).get("name") or "—",
        "assignee": display_name(f.get("assignee")),
        "priority": (f.get("priority") or {}).get("name") or "—",
        "summary": f.get("summary") or "",
        "project": (f.get("project") or {}).get("key") or "",
        "url": f"{jira.server}/browse/{key}",
    }


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def issue_link(key: str, snap: dict) -> str:
    return f'<a href="{esc(snap["url"])}">{esc(key)}</a>'


def compose(event: str, key: str, new: dict, old: dict | None) -> str:
    icon = EVENT_ICONS.get(event, "•")
    title = esc(new["summary"])

    if event == "created":
        return (
            f"{icon} <b>{issue_link(key, new)}</b> {title}\n"
            f"📁 {esc(new['project'])} · 📌 {esc(new['status'])} · "
            f"👤 {esc(new['assignee'])} · ⚡ {esc(new['priority'])}"
        )

    if event == "status":
        return (
            f"{icon} <b>{issue_link(key, new)}</b>: {esc(old['status'])} → <b>{esc(new['status'])}</b>\n"
            f"<i>{title}</i>"
        )

    if event == "assignee":
        return (
            f"{icon} <b>{issue_link(key, new)}</b>: {esc(old['assignee'])} → <b>{esc(new['assignee'])}</b>\n"
            f"<i>{title}</i>"
        )

    if event == "priority":
        return (
            f"{icon} <b>{issue_link(key, new)}</b>: {esc(old['priority'])} → <b>{esc(new['priority'])}</b>\n"
            f"<i>{title}</i>"
        )

    return f"{icon} <b>{issue_link(key, new)}</b> {title}"


def comment_id(comment: dict) -> int:
    try:
        return int(comment.get("id", 0))
    except (TypeError, ValueError):
        return 0


def max_comment_id(comments: list[dict]) -> int:
    return max((comment_id(c) for c in comments), default=0)


def pick_new_comments(comments: list[dict], last_id: int) -> list[dict]:
    fresh = [c for c in comments if comment_id(c) > last_id]
    fresh.sort(key=comment_id)
    return fresh


def fetch_comments(cfg: JiraConfig, headers: dict[str, str], key: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {"startAt": 0, "maxResults": COMMENT_PAGE, "orderBy": "-created"}
    )
    quoted = urllib.parse.quote(key)
    data = http_json(
        f"{cfg.server}/rest/api/2/issue/{quoted}/comment?{params}", headers
    )
    return data.get("comments", [])


def clean_markup(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{code(:[^}]*)?\}|\{noformat(:[^}]*)?\}", "", text)
    text = re.sub(r"\{panel(:[^}]*)?\}|\{quote\}", "", text)
    text = re.sub(r"!([^!\s]+)!", r"\1", text)
    text = re.sub(r"\[([^|\]]+)\|([^\]]+)\]", r"\2 (\1)", text)
    text = re.sub(r"\[([^\]]+)\]", r"\1", text)
    text = re.sub(r"\{\{([^}]+)\}\}", r"\1", text)
    text = re.sub(r"(?m)^h[1-6]\.\s*", "", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"_([^_\n]+)_", r"\1", text)
    text = re.sub(r"\?\?([^?\n]+)\?\?", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def compose_comment(key: str, snap: dict, comment: dict, cfg: JiraConfig) -> str:
    author = display_name(comment.get("author"))
    body = clean_markup(comment.get("body") or "")

    if len(body) > cfg.comment_chars:
        body = body[: cfg.comment_chars - 1] + "…"

    url = f"{snap['url']}?focusedCommentId={comment_id(comment)}"

    return (
        f"💬 <b>{issue_link(key, snap)}</b> · {esc(author)}\n"
        f"<i>{esc(snap['summary'])}</i>\n\n"
        f"{esc(body)}\n"
        f'<a href="{esc(url)}">Комментарий в Jira</a>'
    )


def detect_events(
    old: dict | None, new: dict, cfg: JiraConfig, now: float
) -> list[str]:
    events: list[str] = []

    if old is None:
        if (
            "created" in cfg.events
            and new["created"] >= now - cfg.window_minutes * 60 * 2
        ):
            events.append("created")
        return events

    for event in ("status", "assignee", "priority"):
        if event in cfg.events and old.get(event) != new.get(event):
            events.append(event)

    return events


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "issues": {}}
    try:
        with path.open() as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as err:
        log(f"state is unreadable ({err}), starting empty")
        return {"version": STATE_VERSION, "issues": {}}
    data.setdefault("issues", {})
    return data


def save_state(path: Path, state: dict, now: float) -> None:
    issues = state["issues"]
    for key in [
        k for k, v in issues.items() if now - v.get("updated", 0) > PRUNE_SECONDS
    ]:
        del issues[key]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def telegram_call(
    cfg: TelegramConfig, method: str, payload: dict | None = None
) -> dict:
    if not cfg.token:
        raise SystemExit("telegram.token is not set")

    url = f"{cfg.api_base}/bot{cfg.token}/{method}"
    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")
        raise RuntimeError(f"telegram {method} failed: HTTP {err.code} {body}") from err


def send_message(cfg: TelegramConfig, text: str, dry_run: bool) -> None:
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 1] + "…"

    if dry_run:
        log("DRY-RUN message:\n" + text)
        return

    payload = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if cfg.thread_id:
        payload["message_thread_id"] = cfg.thread_id

    for attempt in (1, 2):
        try:
            telegram_call(cfg, "sendMessage", payload)
            return
        except RuntimeError as err:
            if "429" not in str(err) or attempt == 2:
                raise
            log(f"{err}; retrying in 3s")
            time.sleep(3)


def poll_once(cfg: Config, dry_run: bool, verbose: bool = False) -> int:
    headers = jira_headers(cfg.jira)
    state = load_state(cfg.jira.state_file)
    known: dict = state["issues"]

    issues = jira_search(cfg.jira, headers, cfg.jira.jql)
    now = time.time()
    seeding = cfg.jira.seed and not known
    sent = 0
    comment_budget = cfg.jira.comment_max_per_poll
    comments_on = "comment" in cfg.jira.events

    for issue in issues:
        key = issue["key"]
        new = snapshot(issue, cfg.jira)
        old = known.get(key)

        if (
            old is not None
            and old.get("updated") == new["updated"]
            and old.get("status") == new["status"]
        ):
            continue

        comments: list[dict] = []
        needs_comments = comments_on and (
            seeding or old is None or old.get("updated") != new["updated"]
        )
        if needs_comments:
            comments = fetch_comments(cfg.jira, headers, key)

        if seeding:
            new["comment_id"] = max_comment_id(comments) if comments_on else 0
            known[key] = new
            continue

        for event in detect_events(old, new, cfg.jira, now):
            try:
                send_message(cfg.telegram, compose(event, key, new, old), dry_run)
                sent += 1
            except (RuntimeError, SystemExit) as err:
                log(f"failed to notify {key} ({event}): {err}")
                save_state(cfg.jira.state_file, state, now)
                raise

        if comments_on:
            last_id = (old or {}).get("comment_id")
            fresh = pick_new_comments(comments, last_id or 0)
            fresh = [
                c
                for c in fresh
                if display_name(c.get("author")) not in cfg.jira.authors_exclude
            ]

            if last_id is not None:
                allowed = max(0, min(len(fresh), comment_budget))

                for comment in fresh[:allowed]:
                    try:
                        send_message(
                            cfg.telegram,
                            compose_comment(key, new, comment, cfg.jira),
                            dry_run,
                        )
                        sent += 1
                    except (RuntimeError, SystemExit) as err:
                        log(f"failed to notify {key} (comment): {err}")
                        save_state(cfg.jira.state_file, state, now)
                        raise

                comment_budget -= allowed
                if len(fresh) > allowed:
                    log(
                        f"{key}: {len(fresh) - allowed} new comment(s) skipped (per-poll cap)"
                    )

            new["comment_id"] = max(last_id or 0, max_comment_id(comments))

        known[key] = new

    save_state(cfg.jira.state_file, state, now)

    if seeding:
        log(
            f"first run: seeded {len(known)} issues, nothing sent (set notify.seed = false to get history)"
        )
    elif sent:
        log(f"processed {len(issues)} issues, sent {sent} message(s)")
    elif verbose:
        log(f"processed {len(issues)} issues, nothing to send")

    return sent


def run_loop(cfg: Config, dry_run: bool, once: bool, verbose: bool = False) -> None:
    stop = {"flag": False}

    def handle(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    while not stop["flag"]:
        started = time.time()
        try:
            poll_once(cfg, dry_run, verbose)
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:300]
            log(f"jira request failed: HTTP {err.code} {body}")
        except urllib.error.URLError as err:
            log(f"jira is unreachable: {err.reason}")
        except (RuntimeError, SystemExit) as err:
            log(f"poll failed: {err}")
            if once or not cfg.telegram.token:
                raise

        if once:
            break

        elapsed = time.time() - started
        time.sleep(max(5, cfg.jira.interval - elapsed))


def discover(cfg: Config) -> None:
    data = telegram_call(cfg.telegram, "getUpdates?timeout=0")
    updates = data.get("result", [])

    if not updates:
        print(
            "no updates: write something to the bot (or add it to the chat) and retry"
        )
        return

    seen: dict[str, str] = {}
    for update in updates:
        for kind in ("message", "edited_message", "channel_post"):
            msg = update.get(kind)
            if not msg:
                continue
            chat = msg["chat"]
            seen[str(chat["id"])] = (
                chat.get("title") or chat.get("username") or chat.get("first_name", "")
            )
        if "my_chat_member" in update:
            chat = update["my_chat_member"]["chat"]
            seen[str(chat["id"])] = chat.get("title") or chat.get("username") or ""

    print("chat_id\tname")
    for chat_id, title in seen.items():
        print(f"{chat_id}\t{title}")


def test_message(cfg: Config) -> None:
    if not cfg.telegram.chat_id:
        raise SystemExit("telegram.chat_id is not set (run --discover first)")
    telegram_call(
        cfg.telegram,
        "sendMessage",
        {
            "chat_id": cfg.telegram.chat_id,
            "text": "✅ jira-tg-notify подключён",
            "parse_mode": "HTML",
        },
    )
    print("test message sent")


def main() -> int:
    parser = argparse.ArgumentParser(description="Jira -> Telegram notifier")
    parser.add_argument(
        "-c", "--config", default="~/.config/jira-tg-notify/config.toml"
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single poll and exit"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print messages instead of sending them"
    )
    parser.add_argument(
        "--discover", action="store_true", help="print chat ids the bot can see"
    )
    parser.add_argument(
        "--test", action="store_true", help="send a test message to the configured chat"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log every processed issue"
    )
    args = parser.parse_args()

    cfg = load_config(expand(args.config))

    if args.discover:
        discover(cfg)
        return 0
    if args.test:
        test_message(cfg)
        return 0

    log(
        f"polling {cfg.jira.jql!r} every {cfg.jira.interval}s -> chat {cfg.telegram.chat_id or '(unset)'}"
    )
    run_loop(cfg, args.dry_run, args.once, args.verbose)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
