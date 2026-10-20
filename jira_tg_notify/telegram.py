"""Telegram Bot API client: send messages, list chats, test delivery."""

from __future__ import annotations

import json
import time
import urllib.error

from .config import TelegramConfig
from .http import get_json, post_json

TELEGRAM_LIMIT = 4096
RETRY_DELAY = 3


class TelegramError(RuntimeError):
    def __init__(
        self, method: str, status: int, body: str, retry_after: int | None = None
    ):
        super().__init__(f"telegram {method} failed: HTTP {status} {body}")
        self.status = status
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg

    def _url(self, method: str) -> str:
        if not self.cfg.token:
            raise SystemExit("telegram.token is not set")
        return f"{self.cfg.api_base}/bot{self.cfg.token}/{method}"

    def call(self, method: str, payload: dict | None = None) -> dict:
        url = self._url(method)
        try:
            if payload is None:
                return get_json(url)
            return post_json(url, payload)
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")
            raise TelegramError(method, err.code, body, _retry_after(body)) from err

    def send_message(self, text: str, dry_run: bool = False) -> None:
        if len(text) > TELEGRAM_LIMIT:
            text = text[: TELEGRAM_LIMIT - 1] + "…"

        if dry_run:
            from .log import log

            log("DRY-RUN message:\n" + text)
            return

        payload: dict = {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if self.cfg.thread_id:
            payload["message_thread_id"] = self.cfg.thread_id

        for attempt in (1, 2):
            try:
                self.call("sendMessage", payload)
                return
            except TelegramError as err:
                if err.status != 429 or attempt == 2:
                    raise
                delay = err.retry_after or RETRY_DELAY
                from .log import log

                log(f"{err}; retrying in {delay}s")
                time.sleep(delay)

    def discover_chats(self) -> list[tuple[str, str]]:
        updates = self.call("getUpdates?timeout=0").get("result", [])
        seen: dict[str, str] = {}

        for update in updates:
            for kind in ("message", "edited_message", "channel_post"):
                msg = update.get(kind)
                if not msg:
                    continue
                chat = msg["chat"]
                seen[str(chat["id"])] = (
                    chat.get("title")
                    or chat.get("username")
                    or chat.get("first_name", "")
                )
            if "my_chat_member" in update:
                chat = update["my_chat_member"]["chat"]
                seen[str(chat["id"])] = chat.get("title") or chat.get("username") or ""

        return list(seen.items())

    def send_test(self) -> None:
        if not self.cfg.chat_id:
            raise SystemExit("telegram.chat_id is not set (run --discover first)")
        self.call(
            "sendMessage",
            {
                "chat_id": self.cfg.chat_id,
                "text": "✅ jira-tg-notify подключён",
                "parse_mode": "HTML",
            },
        )


def _retry_after(body: str) -> int | None:
    try:
        value = json.loads(body).get("parameters", {}).get("retry_after")
    except (ValueError, AttributeError):
        return None
    return int(value) if value else None
