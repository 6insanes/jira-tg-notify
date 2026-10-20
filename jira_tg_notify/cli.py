"""Command-line entry point."""

from __future__ import annotations

import argparse

from .commands import Commander
from .config import Config, expand, load_config
from .log import log
from .poller import Poller
from .telegram import TelegramClient

DEFAULT_CONFIG = "~/.config/jira-tg-notify/config.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jira -> Telegram notifier")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(expand(args.config))

    try:
        return dispatch(cfg, args)
    except KeyboardInterrupt:
        return 0


def dispatch(cfg: Config, args: argparse.Namespace) -> int:
    telegram = TelegramClient(cfg.telegram)

    if args.discover:
        chats = telegram.discover_chats()
        if not chats:
            print(
                "no updates: write something to the bot (or add it to the chat) and retry"
            )
            return 0
        print("chat_id\tname")
        for chat_id, title in chats:
            print(f"{chat_id}\t{title}")
        return 0

    if args.test:
        telegram.send_test()
        print("test message sent")
        return 0

    log(
        f"polling {cfg.jira.jql!r} every {cfg.jira.interval}s "
        f"-> chat {cfg.telegram.chat_id or '(unset)'}"
    )
    commander = Commander(cfg, telegram) if cfg.telegram.token else None
    Poller(cfg, telegram=telegram, commander=commander).run(
        args.dry_run, args.once, args.verbose
    )
    return 0
