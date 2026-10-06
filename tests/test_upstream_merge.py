"""Tests for the behaviour merged from upstream (chuajunyu/fetchcanvas): module-only files,
hidden Canvas areas, and name sanitising. Canvas is faked; nothing touches the network or
the real vault."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402

BASE = "https://canvas.test"


def http_error(status):
    response = requests.Response()
    response.status_code = status
    response.reason = {403: "Forbidden", 404: "Not Found", 500: "Server Error"}.get(status, "")
    return requests.exceptions.HTTPError(f"{status}", response=response)


class FakeCanvas:
    """Answers get_json / get_json_paginated by URL path. A value that is an exception is raised."""

    def __init__(self, routes):
        self.routes = routes

    def _lookup(self, url):
        path = url.replace(BASE, "").split("?")[0]
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return value

    def get_json(self, url):
        return self._lookup(url)

    def get_json_paginated(self, url):
        return self._lookup(url)


def canvas_file(file_id, name, folder_id=None):
    return {"id": file_id, "filename": name, "folder_id": folder_id,
            "updated_at": "2026-10-01T00:00:00Z", "url": f"{BASE}/download/{file_id}"}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.patches = [
            patch.object(main, "VAULT_ROOT", str(self.vault)),
            patch.object(main, "CANVAS_BASE_URL", BASE),
            patch.object(main, "PUSH_STATE_PATH", Path(self.tmp.name) / "pushed.json"),
            patch.object(main, "LOCAL_STATE_DIR", Path(self.tmp.name)),
        ]
        for p in self.patches:
            p.start()
        self.downloads = []

        def fake_download(url, save_path):
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            Path(save_path).write_text(url)
            self.downloads.append(save_path)
            return True, None

        self.patches.append(patch.object(main, "download_file", side_effect=fake_download))
        self.patches[-1].start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_course(self, routes, code="TEST101"):
        fake = FakeCanvas(routes)
        with patch.object(main, "get_json", side_effect=fake.get_json), \
             patch.object(main, "get_json_paginated", side_effect=fake.get_json_paginated):
            return main.sync_course(1, code)

    def rel(self, path, code="TEST101"):
        return Path(path).relative_to(self.vault / code / main.COURSE_SUBDIR).as_posix()

    def test_sanitize_keeps_colon_and_replaces_illegal(self):
        self.assertEqual(main.sanitize_name("Chapter 7: Prediction"), "Chapter 7: Prediction")
        self.assertEqual(main.sanitize_name("What? <draft>"), "What- -draft-")
        self.assertEqual(main.sanitize_name("CS4246/CS5446"), "CS4246-CS5446")
        self.assertEqual(main.sanitize_name(" .hidden. "), "hidden")
        self.assertEqual(main.sanitize_name(""), "unnamed")

    def test_module_only_files_are_fetched_once(self):
        routes = {
            "/api/v1/courses/1/folders": [{"id": 10, "name": "course files", "parent_folder_id": None},
                                          {"id": 11, "name": "Week 1", "parent_folder_id": 10}],
            "/api/v1/courses/1/files": [canvas_file(100, "slides.pdf", 11)],
            "/api/v1/courses/1/modules": [{"id": 1, "name": "Week 1"}, {"id": 2, "name": "Recap?"}],
            "/api/v1/courses/1/modules/1/items": [
                {"type": "File", "content_id": 100, "title": "slides", "url": f"{BASE}/api/v1/files/100"},
                {"type": "File", "content_id": 200, "title": "worksheet", "url": f"{BASE}/api/v1/files/200"},
                {"type": "Page", "content_id": 5, "title": "notes"},
            ],
            "/api/v1/courses/1/modules/2/items": [
                {"type": "File", "content_id": 200, "title": "worksheet", "url": f"{BASE}/api/v1/files/200"},
            ],
            "/api/v1/files/200": canvas_file(200, "worksheet.pdf"),
        }
        result = self.run_course(routes)
        self.assertEqual(sorted(self.rel(p) for p in self.downloads),
                         ["Modules/Week 1/worksheet.pdf", "Week 1/slides.pdf"])
        self.assertEqual(result["failed"], [])

    def test_hidden_files_tab_is_not_a_failure(self):
        routes = {
            "/api/v1/courses/1/folders": http_error(403),
            "/api/v1/courses/1/files": http_error(403),
            "/api/v1/courses/1/modules": [{"id": 1, "name": "Lecture 1"}],
            "/api/v1/courses/1/modules/1/items": [
                {"type": "File", "content_id": 300, "title": "l1", "url": f"{BASE}/api/v1/files/300"},
                {"type": "File", "content_id": 301, "title": "locked", "url": f"{BASE}/api/v1/files/301"},
            ],
            "/api/v1/files/300": canvas_file(300, "lecture1.pdf"),
            "/api/v1/files/301": http_error(401),
        }
        result = self.run_course(routes, code="CS4246/CS5446")
        self.assertEqual(result["failed"], [])
        self.assertEqual([self.rel(p, "CS4246-CS5446") for p in self.downloads],
                         ["Modules/Lecture 1/lecture1.pdf"])
        self.assertEqual(len(result["hidden"]), 2)  # the Files listing, plus the locked file
        self.assertIn(("Files", "files listing hidden"), result["hidden"])

    def test_other_canvas_errors_fail_the_course_but_not_the_run(self):
        routes = {"/api/v1/courses/1/folders": http_error(500)}
        result = self.run_course(routes)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(self.downloads, [])

    def test_unmatched_course_codes_warn(self):
        with patch.object(main, "SYNC_ALL_COURSES", False), \
             patch.object(main, "COURSE_CODES", {"CS3249", "CS9999"}), \
             patch("builtins.print") as printed:
            unmatched = main.warn_unmatched_courses([(1, "CS3249"), (2, "NST2030")])
        self.assertEqual(unmatched, ["CS9999"])
        self.assertIn("CS9999", printed.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
