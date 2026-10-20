import json
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jira_tg_notify.config import (  # noqa: E402
    Config,
    JiraConfig,
    TelegramConfig,
    load_config,
    parse_interval,
)
from jira_tg_notify.events import detect_events  # noqa: E402
from jira_tg_notify.formatting import (  # noqa: E402
    clean_markup,
    compose,
    compose_comment,
)
from jira_tg_notify.jira import JiraClient  # noqa: E402
from jira_tg_notify.models import (  # noqa: E402
    IssueSnapshot,
    build_snapshot,
    comment_id,
    epoch,
    pick_new_comments,
)
from jira_tg_notify.poller import Poller  # noqa: E402
from jira_tg_notify.storage import PRUNE_SECONDS, StateStore  # noqa: E402

JIRA_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"
SERVER = "https://jira.example.com"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def make_jira(**overrides) -> JiraConfig:
    base = dict(
        server=SERVER,
        jql="updated >= -10m ORDER BY updated ASC",
        interval=60,
        state_db=tmpdir() / "state.db",
        legacy_state_file=None,
        events={"created", "status", "assignee", "priority"},
        seed=True,
        auth_type="bearer",
        api_token="token",
        login="",
        netrc_host="jira.example.com",
        window_minutes=10,
        comment_chars=400,
        comment_max_per_poll=10,
        authors_exclude=[],
    )
    base.update(overrides)
    return JiraConfig(**base)


def make_config(jira: JiraConfig | None = None) -> Config:
    return Config(
        jira=jira or make_jira(),
        telegram=TelegramConfig(
            token="",
            chat_id="-100",
            thread_id=None,
            api_base="https://api.telegram.org",
        ),
    )


def make_issue(
    key="TEST-1",
    status="На проверке",
    assignee="Иван",
    priority="High",
    updated=None,
    created=None,
):
    now = time.time()

    def to_jira(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(JIRA_FMT)

    return {
        "key": key,
        "fields": {
            "summary": "Проверить <баннер> & размеры",
            "status": {"name": status},
            "assignee": {"displayName": assignee} if assignee else None,
            "priority": {"name": priority},
            "project": {"key": "TEST"},
            "created": to_jira(created if created is not None else now - 3600),
            "updated": to_jira(updated if updated is not None else now),
        },
    }


def make_comment(cid, author="Пётр", body="Текст комментария"):
    return {
        "id": str(cid),
        "author": {"displayName": author, "name": author.lower()},
        "body": body,
        "created": "2026-10-20T10:00:00.000+0300",
    }


class FakeJira:
    def __init__(self):
        self.issues: list[dict] = []
        self.comments: dict[str, list[dict]] = {}
        self.comment_calls: list[str] = []

    def search(self, jql=None):
        return self.issues

    def comment_list(self, key):
        self.comment_calls.append(key)
        return self.comments.get(key, [])


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text, dry_run=False):
        self.sent.append(text)


class PollerTestCase(unittest.TestCase):
    def setUp(self):
        self.jira = make_jira()
        self.config = make_config(self.jira)
        self.feed = FakeJira()
        self.telegram = FakeTelegram()
        self.poller = Poller(
            self.config,
            jira=self.feed,
            telegram=self.telegram,
            log_fn=lambda _msg: None,
        )

    def stored(self) -> dict[str, IssueSnapshot]:
        with StateStore(self.jira.state_db) as store:
            return store.load()


class TestTime(unittest.TestCase):
    def test_epoch(self):
        expected = datetime(2026, 10, 20, 7, 26, 46, tzinfo=timezone.utc).timestamp()
        self.assertEqual(expected, epoch("2026-10-20T10:26:46.000+0300"))

    def test_epoch_bad(self):
        self.assertEqual(0.0, epoch("junk"))
        self.assertEqual(0.0, epoch(None))


class TestCompose(unittest.TestCase):
    def test_created_escapes_html(self):
        snap = build_snapshot(make_issue(), SERVER)
        text = compose("created", "TEST-1", snap, None)

        self.assertIn("TEST-1", text)
        self.assertIn("Проверить &lt;баннер&gt; &amp; размеры", text)
        self.assertIn("https://jira.example.com/browse/TEST-1", text)

    def test_status_arrow(self):
        old = build_snapshot(make_issue(status="На проверке"), SERVER)
        new = build_snapshot(make_issue(status="Готово"), SERVER)
        text = compose("status", "TEST-1", new, old)

        self.assertIn("На проверке → <b>Готово</b>", text)

    def test_priority_and_assignee(self):
        old = build_snapshot(make_issue(assignee="Иван", priority="High"), SERVER)
        new = build_snapshot(make_issue(assignee="Пётр", priority="Low"), SERVER)

        self.assertIn("Иван → <b>Пётр</b>", compose("assignee", "T-1", new, old))
        self.assertIn("High → <b>Low</b>", compose("priority", "T-1", new, old))


class TestDetectEvents(unittest.TestCase):
    def test_new_issue_in_window(self):
        jira = make_jira()
        snap = build_snapshot(make_issue(created=time.time() - 60), SERVER)
        self.assertEqual(["created"], detect_events(None, snap, jira, time.time()))

    def test_new_issue_outside_window(self):
        jira = make_jira()
        snap = build_snapshot(make_issue(created=time.time() - 86400), SERVER)
        self.assertEqual([], detect_events(None, snap, jira, time.time()))

    def test_field_changes(self):
        jira = make_jira()
        old = build_snapshot(
            make_issue(status="A", assignee="Иван", priority="High"), SERVER
        )
        new = build_snapshot(
            make_issue(status="B", assignee="Пётр", priority="Low"), SERVER
        )
        self.assertEqual(
            ["status", "assignee", "priority"],
            detect_events(old, new, jira, time.time()),
        )

    def test_events_filter(self):
        jira = make_jira(events={"status"})
        old = build_snapshot(make_issue(status="A", assignee="Иван"), SERVER)
        new = build_snapshot(make_issue(status="B", assignee="Пётр"), SERVER)
        self.assertEqual(["status"], detect_events(old, new, jira, time.time()))

    def test_no_changes(self):
        jira = make_jira()
        snap = build_snapshot(make_issue(), SERVER)
        self.assertEqual([], detect_events(snap, snap, jira, time.time()))


class TestSearch(unittest.TestCase):
    def test_pagination(self):
        pages = []

        def fake_fetch(url, headers):
            pages.append(url)
            start = int(url.split("startAt=")[1].split("&")[0])
            batch = [{"key": f"TEST-{i}"} for i in range(start, min(start + 100, 250))]
            return {"issues": batch, "total": 250}

        issues = JiraClient(make_jira(), fetch=fake_fetch).search("jql")

        self.assertEqual(250, len(issues))
        self.assertEqual(3, len(pages))


class TestStorage(unittest.TestCase):
    def test_roundtrip_and_prune(self):
        path = tmpdir() / "nested" / "state.db"
        now = time.time()

        with StateStore(path) as store:
            store.upsert("OLD-1", IssueSnapshot(updated=now - PRUNE_SECONDS - 10))
            store.upsert("NEW-1", IssueSnapshot(updated=now, status="Готово"))
            store.prune(now)
            loaded = store.load()

        self.assertNotIn("OLD-1", loaded)
        self.assertEqual("Готово", loaded["NEW-1"].status)

    def test_upsert_updates_existing_row(self):
        path = tmpdir() / "state.db"
        with StateStore(path) as store:
            store.upsert("TEST-1", IssueSnapshot(updated=1, status="A"))
            store.upsert("TEST-1", IssueSnapshot(updated=2, status="B", comment_id=7))
            loaded = store.load()

        self.assertEqual(1, len(loaded))
        self.assertEqual("B", loaded["TEST-1"].status)
        self.assertEqual(7, loaded["TEST-1"].comment_id)

    def test_legacy_json_is_imported_once(self):
        folder = tmpdir()
        legacy = folder / "state.json"
        legacy.write_text(
            '{"version": 1, "issues": {"TEST-1": {"updated": 123, "status": "Done",'
            ' "comment_id": 5, "url": "https://x/browse/TEST-1"}}}'
        )

        with StateStore(folder / "state.db", legacy) as store:
            loaded = store.load()

        self.assertEqual("Done", loaded["TEST-1"].status)
        self.assertEqual(5, loaded["TEST-1"].comment_id)
        self.assertFalse(legacy.exists())
        self.assertTrue((folder / "state.json.migrated").exists())

    def test_unreadable_legacy_json_is_quarantined(self):
        folder = tmpdir()
        legacy = folder / "state.json"
        legacy.write_text("{not json")

        with StateStore(folder / "state.db", legacy) as store:
            self.assertEqual({}, store.load())

        self.assertTrue((folder / "state.json.invalid").exists())


class TestPoll(PollerTestCase):
    def test_seed_sends_nothing_then_events(self):
        self.feed.issues = [make_issue(key="TEST-1", status="Backlog")]
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertIn("TEST-1", self.stored())

        self.feed.issues = [make_issue(key="TEST-1", status="Готово")]
        self.assertEqual(1, self.poller.poll_once(dry_run=True))

        self.feed.issues = [make_issue(key="TEST-2", created=time.time() - 60)]
        self.assertEqual(1, self.poller.poll_once(dry_run=True))

    def test_unchanged_issue_is_skipped(self):
        self.feed.issues = [make_issue(key="TEST-1")]
        self.poller.poll_once(dry_run=True)
        self.assertEqual(0, self.poller.poll_once(dry_run=True))

    def test_state_survives_restart(self):
        self.feed.issues = [make_issue(key="TEST-1", status="Backlog")]
        self.poller.poll_once(dry_run=True)

        restarted = Poller(
            self.config,
            jira=self.feed,
            telegram=self.telegram,
            log_fn=lambda _msg: None,
        )
        self.feed.issues = [make_issue(key="TEST-1", status="Backlog")]
        self.assertEqual(0, restarted.poll_once(dry_run=True))

    def test_legacy_state_import_does_not_reseed(self):
        issue = make_issue(key="TEST-1", status="Backlog")
        legacy = self.jira.state_db.with_suffix(".json")
        legacy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "issues": {"TEST-1": asdict(build_snapshot(issue, SERVER))},
                }
            )
        )
        self.jira.legacy_state_file = legacy
        self.feed.issues = [issue]

        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertEqual(0, self.poller.poll_once(dry_run=True))
        self.assertFalse(legacy.exists())


class TestComments(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.jira.events = {"created", "status", "assignee", "priority", "comment"}

    def poll(self, comments, status="На проверке", key="TEST-1", updated=None):
        self.feed.issues = [make_issue(key=key, status=status, updated=updated)]
        self.feed.comments = {key: comments}
        return self.poller.poll_once(dry_run=True)

    def test_seed_records_last_comment_without_notifying(self):
        self.assertEqual(0, self.poll([make_comment(10)]))
        self.assertEqual(10, self.stored()["TEST-1"].comment_id)

    def test_new_comment_notifies_once(self):
        self.poll([make_comment(10)])
        self.assertEqual(1, self.poll([make_comment(11, author="Анна")]))
        self.assertEqual(0, self.poll([make_comment(11, author="Анна")]))

    def test_excluded_author_is_skipped_but_remembered(self):
        self.poll([make_comment(10)])

        self.jira.authors_exclude = ["integration-bot"]
        self.assertEqual(0, self.poll([make_comment(11, author="integration-bot")]))
        self.assertEqual(1, self.poll([make_comment(12, author="Анна")]))

    def test_many_comments_are_capped(self):
        self.jira.comment_max_per_poll = 1
        self.poll([make_comment(1)])

        comments = [make_comment(cid) for cid in range(2, 6)]
        self.assertEqual(1, self.poll(comments))
        self.assertEqual(5, self.stored()["TEST-1"].comment_id)

    def test_comments_not_fetched_for_unchanged_issue(self):
        updated = time.time()
        self.poll([make_comment(10)], updated=updated)
        self.feed.comment_calls.clear()
        self.assertEqual(0, self.poll([make_comment(10)], updated=updated))
        self.assertEqual([], self.feed.comment_calls)

    def test_clean_markup(self):
        raw = (
            "h2. Заголовок\n"
            "[https://example.com|ссылка] и [https://example.com]\n"
            "{code:java}int x = 1;{code}\n"
            "*жирный* _курсив_ {{код}} !image.png!\n"
            "<br>конец"
        )
        cleaned = clean_markup(raw)

        self.assertIn("Заголовок", cleaned)
        self.assertIn("ссылка (https://example.com)", cleaned)
        self.assertIn("int x = 1;", cleaned)
        self.assertIn("жирный курсив код image.png", cleaned)
        self.assertIn("конец", cleaned)
        self.assertNotIn("{code", cleaned)
        self.assertNotIn("<br>", cleaned)

    def test_pick_new_comments(self):
        comments = [make_comment(5), make_comment(7), make_comment(6)]
        fresh = pick_new_comments(comments, 5)
        self.assertEqual([6, 7], [comment_id(c) for c in fresh])

    def test_compose_comment(self):
        snap = build_snapshot(make_issue(), SERVER)
        text = compose_comment(
            "TEST-1", snap, make_comment(42, author="Пётр <b>"), max_chars=400
        )

        self.assertIn("Пётр &lt;b&gt;", text)
        self.assertIn("focusedCommentId=42", text)
        self.assertIn("Текст комментария", text)

    def test_compose_comment_truncates(self):
        snap = build_snapshot(make_issue(), SERVER)
        text = compose_comment(
            "TEST-1", snap, make_comment(1, body="я" * 100), max_chars=20
        )

        self.assertIn("…", text)
        self.assertNotIn("я" * 30, text)


class TestConfig(unittest.TestCase):
    def test_load_defaults(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[telegram]
token = "123:abc"
chat_id = "-100500"

[jira]
server = "https://jira.example.com/"

[notify]
interval = "2m"
state_file = "~/state/state.json"
"""
        )

        cfg = load_config(path)

        self.assertEqual("https://jira.example.com", cfg.jira.server)
        self.assertEqual(120, cfg.jira.interval)
        self.assertEqual("updated >= -10m ORDER BY updated ASC", cfg.jira.jql)
        self.assertTrue(cfg.jira.seed)
        self.assertEqual("-100500", cfg.telegram.chat_id)
        self.assertIsNone(cfg.telegram.thread_id)
        self.assertEqual("state.db", cfg.jira.state_db.name)
        self.assertEqual("state.json", cfg.jira.legacy_state_file.name)

    def test_state_db_key_wins(self):
        path = tmpdir() / "config.toml"
        path.write_text(
            """
[jira]
server = "https://jira.example.com"

[notify]
state_db = "~/new/state.db"
"""
        )

        cfg = load_config(path)
        self.assertEqual("state.db", cfg.jira.state_db.name)
        self.assertEqual("new", cfg.jira.state_db.parent.name)

    def test_parse_interval(self):
        self.assertEqual(30, parse_interval(30))
        self.assertEqual(30, parse_interval("30s"))
        self.assertEqual(3600, parse_interval("1h"))


if __name__ == "__main__":
    unittest.main()
