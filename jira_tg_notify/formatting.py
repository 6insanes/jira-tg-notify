"""Telegram-facing message text: HTML escaping, Jira wiki cleanup, layouts."""

from __future__ import annotations

import html
import re

from .models import IssueSnapshot, comment_id, display_name

EVENT_ICONS = {
    "created": "🆕",
    "status": "🔄",
    "assignee": "👤",
    "priority": "⚡",
    "comment": "💬",
}


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def issue_link(key: str, snap: IssueSnapshot) -> str:
    return f'<a href="{esc(snap.url)}">{esc(key)}</a>'


def compose(event: str, key: str, new: IssueSnapshot, old: IssueSnapshot | None) -> str:
    icon = EVENT_ICONS.get(event, "•")
    title = esc(new.summary)

    if event == "created":
        return (
            f"{icon} <b>{issue_link(key, new)}</b> {title}\n"
            f"📁 {esc(new.project)} · 📌 {esc(new.status)} · "
            f"👤 {esc(new.assignee)} · ⚡ {esc(new.priority)}"
        )

    if event in ("status", "assignee", "priority") and old is not None:
        before = esc(getattr(old, event))
        after = esc(getattr(new, event))
        return (
            f"{icon} <b>{issue_link(key, new)}</b>: {before} → <b>{after}</b>\n"
            f"<i>{title}</i>"
        )

    return f"{icon} <b>{issue_link(key, new)}</b> {title}"


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


def compose_comment(
    key: str, snap: IssueSnapshot, comment: dict, max_chars: int
) -> str:
    author = display_name(comment.get("author"))
    body = clean_markup(comment.get("body") or "")

    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"

    url = f"{snap.url}?focusedCommentId={comment_id(comment)}"

    return (
        f"💬 <b>{issue_link(key, snap)}</b> · {esc(author)}\n"
        f"<i>{esc(snap.summary)}</i>\n\n"
        f"{esc(body)}\n"
        f'<a href="{esc(url)}">Комментарий в Jira</a>'
    )
