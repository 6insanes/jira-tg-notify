"""Jira REST client: search by JQL and read issue comments."""

from __future__ import annotations

import base64
import netrc
import urllib.parse

from .config import JiraConfig
from .http import get_json

FIELDS = "summary,status,assignee,priority,project,issuetype,created,updated"
PAGE_SIZE = 100
MAX_RESULTS = 1000
COMMENT_PAGE = 100


def netrc_password(host: str) -> tuple[str, str]:
    try:
        auth = netrc.netrc().authenticators(host)
    except (FileNotFoundError, netrc.NetrcParseError):
        return "", ""
    if not auth:
        return "", ""
    login, _, password = auth
    return login or "", password or ""


def auth_header(cfg: JiraConfig) -> dict[str, str]:
    token = cfg.api_token
    login = cfg.login

    if not token:
        login, token = netrc_password(cfg.netrc_host)
    if not token:
        raise SystemExit(
            f"no Jira token: set jira.api_token, JIRA_TOKEN or ~/.netrc entry for {cfg.netrc_host}"
        )

    if cfg.auth_type == "basic":
        raw = base64.b64encode(f"{login}:{token}".encode()).decode()
        value = f"Basic {raw}"
    else:
        value = f"Bearer {token}"

    return {"Authorization": value, "Accept": "application/json"}


class JiraClient:
    """Thin Jira Server/DC API wrapper.

    The HTTP callable is injectable so the poller can be tested without a
    network and without patching module globals.
    """

    def __init__(self, cfg: JiraConfig, fetch=get_json):
        self.cfg = cfg
        self._fetch = fetch
        self._headers = auth_header(cfg)

    def search(self, jql: str | None = None) -> list[dict]:
        issues: list[dict] = []
        start = 0

        while start < MAX_RESULTS:
            params = urllib.parse.urlencode(
                {
                    "jql": jql or self.cfg.jql,
                    "startAt": start,
                    "maxResults": PAGE_SIZE,
                    "fields": FIELDS,
                    "validateQuery": "false",
                }
            )
            data = self._fetch(
                f"{self.cfg.server}/rest/api/2/search?{params}", self._headers
            )

            batch = data.get("issues", [])
            issues.extend(batch)

            total = int(data.get("total", 0))
            start += len(batch)
            if not batch or start >= total:
                break

        return issues

    def comment_list(self, key: str) -> list[dict]:
        params = urllib.parse.urlencode(
            {"startAt": 0, "maxResults": COMMENT_PAGE, "orderBy": "-created"}
        )
        quoted = urllib.parse.quote(key)
        data = self._fetch(
            f"{self.cfg.server}/rest/api/2/issue/{quoted}/comment?{params}",
            self._headers,
        )
        return data.get("comments", [])
