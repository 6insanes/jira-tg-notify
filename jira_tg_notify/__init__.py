"""Jira -> Telegram notifier package.

Layout:
    config      TOML -> dataclasses
    jira        Jira REST client (search, comments)
    telegram    Bot API client (send, discover, test)
    models      IssueSnapshot and comment helpers
    events      which notifications a change produces
    formatting  Telegram HTML message text
    storage     SQLite state store (+ legacy JSON import)
    poller      the poll loop
    commands    Telegram commands for the project blacklist
    cli         argparse entry point
"""

__version__ = "2.4.0"
