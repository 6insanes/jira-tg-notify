"""Domain objects shared by the Jira, storage and formatting layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

JIRA_DATETIME = "%Y-%m-%dT%H:%M:%S.%f%z"
EMPTY = "—"


@dataclass
class IssueSnapshot:
    """What the notifier remembers about an issue between polls."""

    updated: float = 0.0
    created: float = 0.0
    status: str = EMPTY
    assignee: str = EMPTY
    priority: str = EMPTY
    summary: str = ""
    project: str = ""
    url: str = ""
    comment_id: int | None = None


def epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.strptime(value, JIRA_DATETIME).timestamp()
    except ValueError:
        return 0.0


def display_name(person: dict | None) -> str:
    if not person:
        return EMPTY
    return person.get("displayName") or person.get("name") or EMPTY


def same_person(a: dict | None, b: dict | None) -> bool:
    """Match two Jira user objects by username first, then id, then display name."""
    if not a or not b:
        return False
    for field in ("name", "key"):
        if a.get(field) and b.get(field):
            return a[field] == b[field]
    return a.get("displayName") == b.get("displayName")


def build_snapshot(issue: dict, server: str) -> IssueSnapshot:
    fields = issue["fields"]
    key = issue["key"]
    return IssueSnapshot(
        updated=epoch(fields.get("updated")),
        created=epoch(fields.get("created")),
        status=(fields.get("status") or {}).get("name") or EMPTY,
        assignee=display_name(fields.get("assignee")),
        priority=(fields.get("priority") or {}).get("name") or EMPTY,
        summary=fields.get("summary") or "",
        project=(fields.get("project") or {}).get("key") or "",
        url=f"{server}/browse/{key}",
    )


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


def from_legacy(payload: dict) -> IssueSnapshot:
    """Build a snapshot from a pre-SQLite state.json entry."""
    return IssueSnapshot(
        updated=float(payload.get("updated") or 0),
        created=float(payload.get("created") or 0),
        status=payload.get("status") or EMPTY,
        assignee=payload.get("assignee") or EMPTY,
        priority=payload.get("priority") or EMPTY,
        summary=payload.get("summary") or "",
        project=payload.get("project") or "",
        url=payload.get("url") or "",
        comment_id=payload.get("comment_id"),
    )
