import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jira_tg_notify as n  # noqa: E402


def make_jira(**overrides):
    base = dict(
        server="https://jira.example.com",
        jql="updated >= -10m ORDER BY updated ASC",
        interval=60,
        state_file=Path(tempfile.mkdtemp()) / "state.json",
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
    return n.JiraConfig(**base)


def make_issue(
    key="TEST-1",
    status="На проверке",
    assignee="Иван",
    priority="High",
    updated=None,
    created=None,
):
    now = time.time()
    fmt = "%Y-%m-%dT%H:%M:%S.%f%z"

    def to_jira(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)

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


class TestTime(unittest.TestCase):
    def test_epoch(self):
        self.assertEqual(
            datetime(2026, 10, 20, 7, 26, 46, tzinfo=timezone.utc).timestamp(),
            n.epoch("2026-10-20T10:26:46.000+0300"),
        )

    def test_epoch_bad(self):
        self.assertEqual(0.0, n.epoch("junk"))
        self.assertEqual(0.0, n.epoch(None))


class TestCompose(unittest.TestCase):
    def test_created_escapes_html(self):
        snap = n.snapshot(make_issue(), make_jira())
        text = n.compose("created", "TEST-1", snap, None)

        self.assertIn("TEST-1", text)
        self.assertIn("Проверить &lt;баннер&gt; &amp; размеры", text)
        self.assertIn("https://jira.example.com/browse/TEST-1", text)

    def test_status_arrow(self):
        old = n.snapshot(make_issue(status="На проверке"), make_jira())
        new = n.snapshot(make_issue(status="Готово"), make_jira())
        text = n.compose("status", "TEST-1", new, old)

        self.assertIn("На проверке → <b>Готово</b>", text)


class TestDetectEvents(unittest.TestCase):
    def test_new_issue_in_window(self):
        jira = make_jira()
        snap = n.snapshot(make_issue(created=time.time() - 60), jira)
        self.assertEqual(["created"], n.detect_events(None, snap, jira, time.time()))

    def test_new_issue_outside_window(self):
        jira = make_jira()
        snap = n.snapshot(make_issue(created=time.time() - 86400), jira)
        self.assertEqual([], n.detect_events(None, snap, jira, time.time()))

    def test_field_changes(self):
        jira = make_jira()
        old = n.snapshot(
            make_issue(status="На проверке", assignee="Иван", priority="High"), jira
        )
        new = n.snapshot(
            make_issue(status="Готово", assignee="Пётр", priority="Low"), jira
        )
        self.assertEqual(
            ["status", "assignee", "priority"],
            n.detect_events(old, new, jira, time.time()),
        )

    def test_events_filter(self):
        jira = make_jira(events={"status"})
        old = n.snapshot(make_issue(status="На проверке", assignee="Иван"), jira)
        new = n.snapshot(make_issue(status="Готово", assignee="Пётр"), jira)
        self.assertEqual(["status"], n.detect_events(old, new, jira, time.time()))

    def test_no_changes(self):
        jira = make_jira()
        snap = n.snapshot(make_issue(), jira)
        self.assertEqual([], n.detect_events(snap, snap, jira, time.time()))


class TestSearch(unittest.TestCase):
    def test_pagination(self):
        pages = []

        def fake_json(url, headers, timeout=30):
            pages.append(url)
            start = int(url.split("startAt=")[1].split("&")[0])
            batch = [{"key": f"TEST-{i}"} for i in range(start, min(start + 100, 250))]
            return {"issues": batch, "total": 250}

        original = n.http_json
        n.http_json = fake_json
        try:
            issues = n.jira_search(make_jira(), {"Authorization": "x"}, "jql")
        finally:
            n.http_json = original

        self.assertEqual(250, len(issues))
        self.assertEqual(3, len(pages))


class TestState(unittest.TestCase):
    def test_roundtrip_and_prune(self):
        path = Path(tempfile.mkdtemp()) / "nested" / "state.json"
        now = time.time()
        state = {
            "version": n.STATE_VERSION,
            "issues": {
                "OLD-1": {"updated": now - n.PRUNE_SECONDS - 10},
                "NEW-1": {"updated": now},
            },
        }

        n.save_state(path, state, now)
        loaded = n.load_state(path)

        self.assertNotIn("OLD-1", loaded["issues"])
        self.assertIn("NEW-1", loaded["issues"])

    def test_unreadable_state(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        path.write_text("{not json")
        self.assertEqual({}, n.load_state(path)["issues"])


class TestPoll(unittest.TestCase):
    def setUp(self):
        self.state_file = Path(tempfile.mkdtemp()) / "state.json"
        self.jira = make_jira(state_file=self.state_file)
        self.config = n.Config(
            jira=self.jira,
            telegram=n.TelegramConfig(
                token="",
                chat_id="-100",
                thread_id=None,
                api_base="https://api.telegram.org",
            ),
        )

    def patch_search(self, issues):
        original = n.jira_search
        n.jira_search = lambda cfg, headers, jql: issues
        self.addCleanup(lambda: setattr(n, "jira_search", original))

    def test_seed_sends_nothing_then_events(self):
        self.patch_search([make_issue(key="TEST-1", status="Backlog")])
        self.assertEqual(0, n.poll_once(self.config, dry_run=True))
        self.assertIn("TEST-1", n.load_state(self.state_file)["issues"])

        self.patch_search([make_issue(key="TEST-1", status="Готово")])
        self.assertEqual(1, n.poll_once(self.config, dry_run=True))

        self.patch_search([make_issue(key="TEST-2", created=time.time() - 60)])
        self.assertEqual(1, n.poll_once(self.config, dry_run=True))

    def test_unchanged_issue_is_skipped(self):
        self.patch_search([make_issue(key="TEST-1")])
        n.poll_once(self.config, dry_run=True)
        self.assertEqual(0, n.poll_once(self.config, dry_run=True))


def make_comment(cid, author="Пётр", body="Текст комментария"):
    return {
        "id": str(cid),
        "author": {"displayName": author, "name": author.lower()},
        "body": body,
        "created": "2026-10-20T10:00:00.000+0300",
    }


class TestComments(unittest.TestCase):
    def test_clean_markup(self):
        raw = (
            "h2. Заголовок\n"
            "[https://example.com|ссылка] и [https://example.com]\n"
            "{code:java}int x = 1;{code}\n"
            "*жирный* _курсив_ {{код}} !image.png!\n"
            "<br>конец"
        )
        cleaned = n.clean_markup(raw)

        self.assertIn("Заголовок", cleaned)
        self.assertIn("ссылка (https://example.com)", cleaned)
        self.assertIn("int x = 1;", cleaned)
        self.assertIn("жирный курсив код image.png", cleaned)
        self.assertIn("конец", cleaned)
        self.assertNotIn("{code", cleaned)
        self.assertNotIn("<br>", cleaned)

    def test_pick_new_comments(self):
        comments = [make_comment(5), make_comment(7), make_comment(6)]
        fresh = n.pick_new_comments(comments, 5)

        self.assertEqual([6, 7], [n.comment_id(c) for c in fresh])

    def test_compose_comment(self):
        jira = make_jira()
        snap = n.snapshot(make_issue(), jira)
        text = n.compose_comment("TEST-1", snap, make_comment(42, author="Пётр <b>"), jira)

        self.assertIn("Пётр &lt;b&gt;", text)
        self.assertIn("focusedCommentId=42", text)
        self.assertIn("Текст комментария", text)

    def test_compose_comment_truncates(self):
        jira = make_jira(comment_chars=20)
        snap = n.snapshot(make_issue(), jira)
        text = n.compose_comment("TEST-1", snap, make_comment(1, body="я" * 100), jira)

        self.assertIn("…", text)
        self.assertNotIn("я" * 30, text)


class TestCommentPoll(unittest.TestCase):
    def setUp(self):
        self.state_file = Path(tempfile.mkdtemp()) / "state.json"
        self.jira = make_jira(
            state_file=self.state_file,
            events={"created", "status", "assignee", "priority", "comment"},
        )
        self.config = n.Config(
            jira=self.jira,
            telegram=n.TelegramConfig(
                token="",
                chat_id="-100",
                thread_id=None,
                api_base="https://api.telegram.org",
            ),
        )

        original_search = n.jira_search
        original_fetch = n.fetch_comments
        self.addCleanup(lambda: setattr(n, "jira_search", original_search))
        self.addCleanup(lambda: setattr(n, "fetch_comments", original_fetch))

    def poll(self, comments, status="На проверке", key="TEST-1", updated=None):
        issue = make_issue(key=key, status=status, updated=updated)
        n.jira_search = lambda cfg, headers, jql: [issue]
        n.fetch_comments = lambda cfg, headers, issue_key: comments
        return n.poll_once(self.config, dry_run=True)

    def test_seed_records_last_comment_without_notifying(self):
        self.assertEqual(0, self.poll([make_comment(10)]))

        state = n.load_state(self.state_file)
        self.assertEqual(10, state["issues"]["TEST-1"]["comment_id"])

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

        state = n.load_state(self.state_file)
        self.assertEqual(5, state["issues"]["TEST-1"]["comment_id"])


class TestConfig(unittest.TestCase):
    def test_load_defaults(self):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        path.write_text(
            """
[telegram]
token = "123:abc"
chat_id = "-100500"

[jira]
server = "https://jira.example.com/"

[notify]
interval = "2m"
"""
        )

        cfg = n.load_config(path)

        self.assertEqual("https://jira.example.com", cfg.jira.server)
        self.assertEqual(120, cfg.jira.interval)
        self.assertEqual("updated >= -10m ORDER BY updated ASC", cfg.jira.jql)
        self.assertTrue(cfg.jira.seed)
        self.assertEqual("-100500", cfg.telegram.chat_id)
        self.assertIsNone(cfg.telegram.thread_id)


if __name__ == "__main__":
    unittest.main()
