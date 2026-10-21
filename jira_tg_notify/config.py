"""Configuration model and TOML loading."""

from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STATE_DB = "~/.local/state/jira-tg-notify/state.db"
DEFAULT_LEGACY_STATE = "~/.local/state/jira-tg-notify/state.json"
DEFAULT_TODO_JQL = (
    "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
)
ORDER_BY = re.compile(r"\s+ORDER\s+BY\s+", re.IGNORECASE)
ALL_EVENTS = ("created", "status", "assignee", "priority", "comment")

MUTED_SETTING = "projects_muted"
UNMUTED_SETTING = "projects_unmuted"
OFFSET_SETTING = "telegram_offset"
LAST_POLL_SETTING = "last_poll_at"


@dataclass
class JiraConfig:
    server: str
    jql: str
    interval: int
    state_db: Path
    events: set[str]
    seed: bool
    auth_type: str
    api_token: str
    login: str
    netrc_host: str
    window_minutes: int
    comment_chars: int
    comment_max_per_poll: int
    authors_exclude: list[str] = field(default_factory=list)
    projects_exclude: list[str] = field(default_factory=list)
    ignore_self: bool = False
    only_involved: bool = False
    catchup_hours: int = 24
    todo_jql: str = DEFAULT_TODO_JQL
    todo_limit: int = 15
    todo_exclude_statuses: list[str] = field(default_factory=list)
    legacy_state_file: Path | None = None


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


def normalize_project_key(value: str) -> str:
    return str(value or "").strip().upper()


def todo_query(cfg: JiraConfig) -> str:
    """The todo JQL with the statuses that are not your move appended.

    The condition lands before ``ORDER BY`` so ``notify.todo_jql`` keeps its
    sorting; a query without ``ORDER BY`` is parenthesised first, otherwise a
    top-level ``OR`` would swallow the new condition.
    """
    statuses = [str(s).strip() for s in cfg.todo_exclude_statuses if str(s).strip()]
    if not statuses:
        return cfg.todo_jql

    quoted = ", ".join('"' + s.replace('"', '\\"') + '"' for s in statuses)
    condition = f"status NOT IN ({quoted})"

    match = ORDER_BY.search(cfg.todo_jql)
    if match is None:
        return f"({cfg.todo_jql}) AND {condition}"

    return (
        f"{cfg.todo_jql[: match.start()]} AND {condition}"
        f"{cfg.todo_jql[match.start() :]}"
    )


def effective_exclusions(base, muted=(), unmuted=()) -> set[str]:
    """Config keys plus picks from Telegram, minus the ones switched back on."""
    keys = {normalize_project_key(p) for p in base}
    keys |= {normalize_project_key(p) for p in muted}
    keys -= {normalize_project_key(p) for p in unmuted}
    keys.discard("")
    return keys


def parse_interval(value) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1:] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    return int(text)


def state_paths(notify: dict) -> tuple[Path, Path | None]:
    """Resolve the SQLite path and the legacy JSON state to import from.

    ``state_db`` wins; otherwise ``state_file`` keeps working by switching its
    suffix to ``.db``. The matching ``.json`` file is imported once and then
    renamed, so an upgrade does not replay the whole history into Telegram.
    """
    if notify.get("state_db"):
        db = expand(notify["state_db"])
        legacy = expand(notify.get("state_file") or str(db.with_suffix(".json")))
    elif notify.get("state_file"):
        legacy = expand(notify["state_file"])
        db = legacy.with_suffix(".db")
    else:
        db = expand(DEFAULT_STATE_DB)
        legacy = expand(DEFAULT_LEGACY_STATE)

    return db, (legacy if legacy != db else None)


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

    window_minutes = int(nt.get("window_minutes", 10))
    state_db, legacy_state_file = state_paths(nt)

    jira = JiraConfig(
        server=server,
        jql=nt.get("jql") or f"updated >= -{window_minutes}m ORDER BY updated ASC",
        interval=parse_interval(nt.get("interval", 60)),
        state_db=state_db,
        legacy_state_file=legacy_state_file,
        events=set(nt.get("events", ALL_EVENTS)),
        seed=bool(nt.get("seed", True)),
        auth_type=(jr.get("auth_type") or "bearer").lower(),
        api_token=os.environ.get("JIRA_TOKEN") or jr.get("api_token") or "",
        login=jr.get("login") or "",
        netrc_host=jr.get("netrc_host") or urllib.parse.urlparse(server).hostname or "",
        window_minutes=window_minutes,
        comment_chars=int(nt.get("comment_chars", 400)),
        comment_max_per_poll=int(nt.get("comment_max_per_poll", 10)),
        authors_exclude=list(nt.get("authors_exclude", [])),
        projects_exclude=list(nt.get("projects_exclude", [])),
        ignore_self=bool(nt.get("ignore_self", False)),
        only_involved=bool(nt.get("only_involved", False)),
        catchup_hours=int(nt.get("catchup_hours", 24)),
        todo_jql=nt.get("todo_jql") or DEFAULT_TODO_JQL,
        todo_limit=int(nt.get("todo_limit", 15)),
        todo_exclude_statuses=list(nt.get("todo_exclude_statuses", [])),
    )

    telegram = TelegramConfig(
        token=os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("token") or "",
        chat_id=str(os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id") or ""),
        thread_id=tg.get("message_thread_id") or None,
        api_base=(tg.get("api_base") or "https://api.telegram.org").rstrip("/"),
    )

    return Config(jira=jira, telegram=telegram)
