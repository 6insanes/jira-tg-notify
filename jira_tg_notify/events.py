"""Deciding which notifications an issue change produces."""

from __future__ import annotations

from .config import JiraConfig
from .models import IssueSnapshot

FIELD_EVENTS = ("status", "assignee", "priority")
CREATED_GRACE_SECONDS = 2


def detect_events(
    old: IssueSnapshot | None, new: IssueSnapshot, cfg: JiraConfig, now: float
) -> list[str]:
    if old is None:
        in_window = new.created >= now - cfg.window_minutes * 60 * CREATED_GRACE_SECONDS
        return ["created"] if "created" in cfg.events and in_window else []

    return [
        event
        for event in FIELD_EVENTS
        if event in cfg.events and getattr(old, event) != getattr(new, event)
    ]
