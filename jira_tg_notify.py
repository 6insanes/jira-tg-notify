#!/usr/bin/env python3
"""Entry point kept at the repo root so systemd and docs keep working."""

import sys

from jira_tg_notify.cli import main

if __name__ == "__main__":
    sys.exit(main())
