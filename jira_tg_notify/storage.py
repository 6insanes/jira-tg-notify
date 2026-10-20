"""SQLite-backed issue state, with one-time import of the legacy JSON file."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .log import log
from .models import IssueSnapshot, from_legacy

PRUNE_SECONDS = 30 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    key        TEXT PRIMARY KEY,
    updated    REAL NOT NULL DEFAULT 0,
    created    REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT '—',
    assignee   TEXT NOT NULL DEFAULT '—',
    priority   TEXT NOT NULL DEFAULT '—',
    summary    TEXT NOT NULL DEFAULT '',
    project    TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    comment_id INTEGER
);
CREATE INDEX IF NOT EXISTS issues_updated ON issues(updated);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

UPSERT = """
INSERT INTO issues (key, updated, created, status, assignee, priority, summary,
                    project, url, comment_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
    updated = excluded.updated,
    created = excluded.created,
    status = excluded.status,
    assignee = excluded.assignee,
    priority = excluded.priority,
    summary = excluded.summary,
    project = excluded.project,
    url = excluded.url,
    comment_id = excluded.comment_id
"""

COLUMNS = (
    "updated",
    "created",
    "status",
    "assignee",
    "priority",
    "summary",
    "project",
    "url",
    "comment_id",
)


class StateStore:
    """Load, update and prune issue snapshots.

    Used as a context manager so the SQLite connection is always closed::

        with StateStore(cfg.state_db, cfg.legacy_state_file) as store:
            known = store.load()
    """

    def __init__(
        self,
        path: Path,
        legacy_json: Path | None = None,
        log_fn=log,
    ):
        self.path = path
        self.legacy_json = legacy_json
        self._log = log_fn
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "StateStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._import_legacy()
        return self

    def __exit__(self, *_exc) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("StateStore used outside its context manager")
        return self._db

    def load(self) -> dict[str, IssueSnapshot]:
        rows = self.db.execute(
            f"SELECT key, {', '.join(COLUMNS)} FROM issues"
        ).fetchall()
        return {
            row["key"]: IssueSnapshot(**{column: row[column] for column in COLUMNS})
            for row in rows
        }

    def upsert(self, key: str, snap: IssueSnapshot) -> None:
        self.db.execute(UPSERT, (key, *(getattr(snap, c) for c in COLUMNS)))
        self.db.commit()

    def prune(self, now: float, max_age: int = PRUNE_SECONDS) -> int:
        cursor = self.db.execute(
            "DELETE FROM issues WHERE updated < ?", (now - max_age,)
        )
        self.db.commit()
        return cursor.rowcount

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM issues").fetchone()[0])

    def get_setting(self, key: str, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_setting(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    def _import_legacy(self) -> None:
        if self.legacy_json is None or not self.legacy_json.exists():
            return
        if self.count():
            self._log(f"state db already has data, ignoring legacy {self.legacy_json}")
            return

        try:
            data = json.loads(self.legacy_json.read_text())
            issues = data.get("issues", {})
        except (ValueError, OSError) as err:
            self._log(f"legacy state unreadable ({err}), starting empty")
            self._quarantine(
                self.legacy_json.with_name(self.legacy_json.name + ".invalid")
            )
            return

        for key, payload in issues.items():
            self.upsert(key, from_legacy(payload))

        self._log(
            f"migrated {len(issues)} issue(s) from {self.legacy_json} to {self.path}"
        )
        target = self.legacy_json.with_name(self.legacy_json.name + ".migrated")
        self._quarantine(target)

    def _quarantine(self, target: Path) -> None:
        try:
            self.legacy_json.rename(target)
        except OSError as err:
            self._log(f"cannot rename legacy state ({err})")
