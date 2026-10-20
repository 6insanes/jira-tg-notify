"""Telegram commands for the project blacklist.

The config file holds the base list; commands only add to it or switch its
entries off, so ``notify.projects_exclude`` keeps working as a default.
"""

from __future__ import annotations

import re
import urllib.error

from .config import (
    MUTED_SETTING,
    OFFSET_SETTING,
    UNMUTED_SETTING,
    Config,
    effective_exclusions,
    normalize_project_key,
)
from .log import log
from .storage import StateStore
from .telegram import TelegramClient

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

HELP = (
    "<b>Чёрный список проектов</b>\n"
    "/projects — что заглушено сейчас\n"
    "/mute OPS TOSH — заглушить проекты\n"
    "/unmute OPS — вернуть уведомления\n\n"
    "Ключи как в задачах: OPS-123 → OPS. "
    "Список из config.toml остаётся базовым, команды его дополняют и перекрывают."
)

COMMANDS = (
    ("projects", "показать заглушённые проекты"),
    ("mute", "заглушить проекты: /mute OPS TOSH"),
    ("unmute", "вернуть уведомления: /unmute OPS"),
    ("help", "как управлять списком"),
)


class Commander:
    """Reads bot updates and applies /projects, /mute, /unmute."""

    def __init__(
        self,
        cfg: Config,
        telegram: TelegramClient | None = None,
        log_fn=log,
    ):
        self.cfg = cfg
        self.telegram = telegram or TelegramClient(cfg.telegram)
        self.log = log_fn

    def register(self) -> None:
        """Publish the command menu; failure only costs the hint in the UI."""
        try:
            self.telegram.set_commands(COMMANDS)
        except (RuntimeError, SystemExit, urllib.error.URLError) as err:
            self.log(f"cannot publish bot commands: {err}")

    def handle(self, timeout: int = 0) -> int:
        """Consume pending updates and return how many commands were answered."""
        jcfg = self.cfg.jira
        with StateStore(
            jcfg.state_db, jcfg.legacy_state_file, log_fn=self.log
        ) as store:
            offset = store.get_setting(OFFSET_SETTING) or 0
            start = offset
            handled = 0
            for update in self.telegram.get_updates(offset, timeout):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                if self._is_ours(message):
                    handled += self._dispatch(message, store)
            if offset != start:
                store.set_setting(OFFSET_SETTING, offset)
        return handled

    def _is_ours(self, message: dict) -> bool:
        chat_id = str(self.cfg.telegram.chat_id or "")
        chat = str((message.get("chat") or {}).get("id") or "")
        return bool(chat_id) and chat_id == chat

    def _dispatch(self, message: dict, store: StateStore) -> int:
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return 0

        head, _, tail = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        args = [part for arg in tail.split() for part in arg.split(",") if part]

        if command in ("/projects", "/list"):
            self._reply(self._render(store))
            return 1
        if command == "/mute":
            return self._change(store, args, mute=True)
        if command == "/unmute":
            return self._change(store, args, mute=False)

        self._reply(HELP)
        return 1

    def _change(self, store: StateStore, args: list[str], *, mute: bool) -> int:
        name = "/mute" if mute else "/unmute"
        if not args:
            self._reply(f"Укажи ключ проекта: {name} OPS\n\n{HELP}")
            return 1

        keys = [normalize_project_key(arg) for arg in args]
        bad = [key for key in keys if not PROJECT_KEY.match(key)]
        if bad:
            self._reply(
                f"Не похоже на ключ проекта: {', '.join(bad)}. "
                "Нужен ключ из задачи, например OPS."
            )
            return 1

        muted = set(store.get_setting(MUTED_SETTING, []))
        unmuted = set(store.get_setting(UNMUTED_SETTING, []))
        if mute:
            muted |= set(keys)
            unmuted -= set(keys)
            head = f"Заглушил: {', '.join(keys)}."
        else:
            unmuted |= set(keys)
            muted -= set(keys)
            head = f"Вернул уведомления: {', '.join(keys)}."

        store.set_setting(MUTED_SETTING, sorted(muted))
        store.set_setting(UNMUTED_SETTING, sorted(unmuted))
        self._reply(f"{head}\n\n{self._render(store)}")
        return 1

    def _render(self, store: StateStore) -> str:
        jcfg = self.cfg.jira
        keys = sorted(
            effective_exclusions(
                jcfg.projects_exclude,
                store.get_setting(MUTED_SETTING, []),
                store.get_setting(UNMUTED_SETTING, []),
            )
        )
        if not keys:
            return "Чёрный список пуст — уведомления приходят по всем проектам."
        return f"Заглушено ({len(keys)}):\n" + ", ".join(keys) + "\n\n" + HELP

    def _reply(self, text: str) -> None:
        self.telegram.send_message(text)
