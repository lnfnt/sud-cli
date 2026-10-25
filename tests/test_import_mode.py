"""Tests for the manual import CLI command (no network, no cron, no Telegram)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.cli import main  # noqa: E402

MOS_FIXTURES = REPO_ROOT / "mos-spike" / "fixtures"
HAPPY_HTML = MOS_FIXTURES / "case-02-0387-2025.redacted.html"
WAF_HTML = MOS_FIXTURES / "waf.html"
EMPTY_HTML = MOS_FIXTURES / "empty.html"
NOW = "2026-09-06T00:00:00Z"


class ImportHappyPathTests(unittest.TestCase):
    def test_import_ok_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                "--now", NOW,
                "import", str(HAPPY_HTML),
                "--case", "02-0387/2025",
                "--json", f"{tmp}/out.json",
            ])
            self.assertEqual(rc, 0)
            data = json.loads(Path(f"{tmp}/out.json").read_text())
            self.assertEqual(data["case_number"], "02-0387/2025")
            self.assertEqual(data["court"], "Хамовнический районный суд")
            self.assertEqual(len(data["events_timeline"]), 8)

    def test_import_ok_no_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                "--now", NOW,
                "import", str(HAPPY_HTML),
                "--case", "02-0387/2025",
            ])
            self.assertEqual(rc, 0)


class ImportFailClosedTests(unittest.TestCase):
    def test_import_waf_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                "--now", NOW,
                "import", str(WAF_HTML),
                "--case", "02-0387/2025",
            ])
            self.assertEqual(rc, 2)

    def test_import_empty_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                "--now", NOW,
                "import", str(EMPTY_HTML),
                "--case", "02-0387/2025",
            ])
            self.assertEqual(rc, 3)

    def test_import_missing_file_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                "--now", NOW,
                "import", "/nonexistent/file.html",
                "--case", "02-0387/2025",
            ])
            self.assertEqual(rc, 3)

    def test_import_case_mismatch_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                "--now", NOW,
                "import", str(HAPPY_HTML),
                "--case", "99-9999/2099",
            ])
            self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
