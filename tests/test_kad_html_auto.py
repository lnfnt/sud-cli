"""KAD saved-HTML (Ctrl+S) import + auto-import router tests
(colleague-legal feedback 1.1 / 1.2)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.kad.html_import import (  # noqa: E402
    KadHtmlImportProvider, fetch_kad_saved_html)
from case_search.cli import _detect_source  # noqa: E402

SAVED = REPO_ROOT / "fixtures" / "kad-saved-card-А40-308035-2025.html"
NOW = "2026-09-09T00:00:00Z"


class KadSavedHtmlTests(unittest.TestCase):
    def test_saved_card_parses_like_live(self):
        out = fetch_kad_saved_html(
            KadHtmlImportProvider(SAVED, "А40-308035/2025"),
            "А40-308035/2025", NOW)
        self.assertEqual(out.status, "ok", out.reason)
        s = out.snapshot
        self.assertEqual(s.case_number, "А40-308035/2025")
        self.assertEqual(s.court, "АС города Москвы")
        self.assertEqual(s.judge, "Шуваева М. В.")
        self.assertEqual(s.parties.plaintiff, ("POD Trading LLC",))
        self.assertEqual(s.parties.respondent, ("Interactive Brokers LLC",))
        self.assertEqual(s.next_hearing.date, "2026-10-21")

    def test_latin_ref_accepted(self):
        out = fetch_kad_saved_html(
            KadHtmlImportProvider(SAVED, "A40-308035/2025"),
            "A40-308035/2025", NOW)
        self.assertEqual(out.status, "ok", out.reason)


class AutoDetectTests(unittest.TestCase):
    def test_detect_kinds(self):
        self.assertEqual(_detect_source(
            REPO_ROOT / "fixtures"
            / "kad-live-extract-А40-308035-2025.json"), "kad")
        self.assertEqual(_detect_source(SAVED), "kad-html")
        self.assertEqual(_detect_source(
            REPO_ROOT / "fixtures"
            / "sudact" / "arbitral-doc-asmo-2026.html"), "sudact")
        self.assertEqual(_detect_source(
            REPO_ROOT / "fixtures" / "vsrf"
            / "presidium-272-PEK25-A40-217461-2024.pdf"), "vsrf")

    def test_unknown_file_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.txt"
            junk.write_text("no markers here", encoding="utf-8")
            with self.assertRaises(ValueError):
                _detect_source(junk)


if __name__ == "__main__":
    unittest.main()
