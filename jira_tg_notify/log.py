"""Timestamped logging to stderr (journal-friendly)."""

from __future__ import annotations

import sys
from datetime import datetime


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)
