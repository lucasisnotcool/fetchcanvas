"""Regression tests for local-first FetchCanvas publication behaviour."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_upcoming  # noqa: E402
import main  # noqa: E402


class MainResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_pending = main.PENDING_CHANGELOG_PATH
        self.original_state = main.RUN_STATE_PATH
        self.original_output = main.OUTPUT_PATH
        main.PENDING_CHANGELOG_PATH = self.root / "pending-changelog.md"
        main.RUN_STATE_PATH = self.root / "last-run.json"
        main.OUTPUT_PATH = str(self.root / "output")

    def tearDown(self):
        main.PENDING_CHANGELOG_PATH = self.original_pending
        main.RUN_STATE_PATH = self.original_state
        main.OUTPUT_PATH = self.original_output
        self.temp_dir.cleanup()

    @staticmethod
    def result(downloaded=None, updated=None, skipped=None, failed=None):
        return {
            "course_code": "TEST101",
            "downloaded": downloaded or [],
            "updated": updated or [],
            "skipped": skipped or [],
            "failed": failed or [],
        }

    def test_no_change_run_does_not_touch_remote_changelog(self):
        clean = self.result(skipped=[("handout.pdf", "TEST101")])
        with patch.object(main, "SYNC_ALL_COURSES", True), \
             patch.object(main, "get_all_courses", return_value=[(1, "TEST101")]), \
             patch.object(main, "sync_course", return_value=clean), \
             patch.object(main, "update_changelog") as update_changelog, \
             patch("fetch_upcoming.main", return_value={"ok": True, "error": None, "targets": []}):
            self.assertEqual(main.run(), 0)

        update_changelog.assert_not_called()
        state = json.loads(main.RUN_STATE_PATH.read_text())
        self.assertEqual(state["changelog"]["status"], "not_needed")

    def test_unavailable_changelog_is_queued_locally(self):
        changed = self.result(downloaded=[("handout.pdf", "TEST101")])
        with patch.object(main, "SYNC_ALL_COURSES", True), \
             patch.object(main, "get_all_courses", return_value=[(1, "TEST101")]), \
             patch.object(main, "sync_course", return_value=changed), \
             patch.object(main, "update_changelog", return_value=(False, "Resource deadlock avoided")), \
             patch("fetch_upcoming.main", return_value={"ok": True, "error": None, "targets": []}):
            self.assertEqual(main.run(), main.METADATA_DEFERRED_EXIT_CODE)

        queued = main.PENDING_CHANGELOG_PATH.read_text()
        self.assertIn("fetchcanvas-event", queued)

    def test_changelog_replay_is_idempotent(self):
        run_at = datetime(2026, 8, 16, tzinfo=main.TIMEZONE)
        entry = main.build_run_entry(
            run_at,
            [("TEST101", ["- Downloaded (new): 1"])],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(main.update_changelog(str(self.root), run_at, entry), (True, None))
        changelog = self.root / main.CHANGELOG_NAME
        first_write = changelog.read_text()
        self.assertEqual(main.update_changelog(str(self.root), run_at, entry), (True, None))
        self.assertEqual(changelog.read_text(), first_write)


class UpcomingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.original_output = fetch_upcoming.OUTPUT_PATH
        self.original_vault = fetch_upcoming.VAULT
        self.original_state = fetch_upcoming.PUBLISH_STATE_PATH
        fetch_upcoming.OUTPUT_PATH = str(self.root / "output")
        fetch_upcoming.VAULT = str(self.vault)
        fetch_upcoming.PUBLISH_STATE_PATH = self.root / "state" / "upcoming.json"
        self.now = datetime(2026, 8, 16, 10, tzinfo=fetch_upcoming.SGT)
        self.items = [{
            "course": "TEST101",
            "name": "Assignment",
            "due_at_sgt": "Mon 17 Aug 2026 12:00",
            "due_iso": "2026-08-17T12:00:00+08:00",
            "points": 10,
            "submitted": False,
            "graded": False,
            "url": "https://example.invalid/assignment",
        }]

    def tearDown(self):
        fetch_upcoming.OUTPUT_PATH = self.original_output
        fetch_upcoming.VAULT = self.original_vault
        fetch_upcoming.PUBLISH_STATE_PATH = self.original_state
        self.temp_dir.cleanup()

    def test_unchanged_data_does_not_rewrite_cloud_snapshots(self):
        with patch.object(fetch_upcoming, "collect", return_value=(self.now, self.items, [])):
            first = fetch_upcoming.main()
            self.assertTrue(first["ok"])
            self.assertEqual(len(first["targets"]), 3)
            snapshot = (self.root / "output" / "UPCOMING.md").read_text()

            second = fetch_upcoming.main()
            self.assertTrue(second["ok"])
            self.assertEqual(second["targets"], [])
            self.assertEqual((self.root / "output" / "UPCOMING.md").read_text(), snapshot)

    def test_incomplete_collection_preserves_previous_snapshot(self):
        with patch.object(fetch_upcoming, "collect", return_value=(self.now, self.items, [])):
            fetch_upcoming.main()
        snapshot_path = self.root / "output" / "UPCOMING.md"
        previous = snapshot_path.read_text()

        with patch.object(fetch_upcoming, "collect", return_value=(self.now, [], ["TEST101 unavailable"])):
            result = fetch_upcoming.main()

        self.assertFalse(result["ok"])
        self.assertEqual(snapshot_path.read_text(), previous)


if __name__ == "__main__":
    unittest.main()
