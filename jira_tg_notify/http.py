"""Minimal JSON-over-HTTP helper shared by the Jira and Telegram clients."""

from __future__ import annotations

import json
import urllib.request

DEFAULT_TIMEOUT = 30


def get_json(
    url: str, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT
) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    body = json.dumps(payload).encode()
    merged = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=merged, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)
