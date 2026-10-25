"""KAD chronology + instances tests (colleague-legal feedback 2.1/2.2)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.kad.html_import import (  # noqa: E402
    KadHtmlImportProvider, parse_kad_saved_html, fetch_kad_saved_html)

SAVED = REPO_ROOT / "fixtures" / "kad-saved-card-А40-308035-2025.html"
NOW = "2026-09-09T00:00:00Z"


class ChronologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = SAVED.read_text(encoding="utf-8")
        cls.data = parse_kad_saved_html(
            cls.html, case_ref="А40-308035/2025", source="s",
            source_url="u", retrieved_at=NOW, raw_artifact_hash="h")

    def test_events_extracted(self):
        self.assertGreater(len(self.data["events"]), 5)
        first = self.data["events"][0]
        self.assertEqual(first["date"], "29.04.2026")
        self.assertEqual(first["type"], "Определение")
        self.assertTrue(first["pdf"])

    def test_instances_extracted_no_js_templates(self):
        inst = self.data["instances"]
        self.assertTrue(inst)
        for i in inst:
            self.assertNotIn("$", i["level"])
        self.assertEqual(inst[0]["court"], "АС города Москвы")
        self.assertTrue(inst[0]["instance_id"])

    def test_timeline_in_snapshot(self):
        out = fetch_kad_saved_html(
            KadHtmlImportProvider(SAVED, "А40-308035/2025"),
            "А40-308035/2025", NOW)
        self.assertEqual(out.status, "ok")
        tl = out.snapshot.events_timeline
        self.assertGreater(len(tl), 5)
        dates = [e.date for e in tl]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(any(e.published for e in tl))


if __name__ == "__main__":
    unittest.main()
