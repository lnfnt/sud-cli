"""ГАС Правосудие (sudrf) import parser tests — live fixture."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (EmptyPayloadError,  # noqa: E402
                                  MalformedPayloadError)
from case_search.sudrf.parser import (fetch_sudrf_html,  # noqa: E402
                                        parse_sudrf_html,
                                        SudrfImportProvider)

FIXTURE = (REPO_ROOT / "fixtures" / "sudrf"
           / "gas-print-2a-2939-2025.html")
NOW = "2026-09-10T00:00:00Z"


class GasParserTests(unittest.TestCase):
    def test_live_fixture_parses(self):
        out = fetch_sudrf_html(
            SudrfImportProvider(FIXTURE, "33а-29651/2025"),
            "33а-29651/2025", NOW)
        self.assertEqual(out.status, "ok", out.reason)
        s = out.snapshot
        self.assertEqual(s.case_number, "33а-29651/2025")
        self.assertEqual(s.court, "Московский областной суд")
        self.assertEqual(s.judge, "Белая Светлана Львовна")
        self.assertIn("БЕЗ ИЗМЕНЕНИЯ", s.status)
        self.assertEqual(len(s.events_timeline), 1)
        self.assertEqual(s.events_timeline[0].date, "2025-08-20")

    def test_identity_mismatch_fails_closed(self):
        out = fetch_sudrf_html(
            SudrfImportProvider(FIXTURE, "99-9999/2099"),
            "99-9999/2099", NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("mismatch", out.reason)

    def test_missing_file_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            provider = SudrfImportProvider(
                Path(tmp) / "nope.html", "33а-29651/2025")
            out = fetch_sudrf_html(provider, "33а-29651/2025", NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("missing", out.reason)

    def test_empty_payload_fails_closed(self):
        with self.assertRaises(EmptyPayloadError):
            parse_sudrf_html("  ", case_ref="33а-29651/2025", source="s",
                             source_url="u", retrieved_at=NOW,
                             raw_artifact_hash="h")

    def test_full_text_extraction(self):
        from case_search.sudrf.parser import extract_full_text
        html = FIXTURE.read_bytes().decode("cp1251", errors="replace")
        text = extract_full_text(html)
        self.assertGreater(len(text), 5000)
        self.assertIn("Балашихинский городской суд", text)
        self.assertIn("оставить без изменения", text)


if __name__ == "__main__":
    unittest.main()
