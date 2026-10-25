"""Case-ref normalization tests (colleague-legal feedback 1.4)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.normalize_refs import (normalize_case_ref,  # noqa: E402
                                          refs_match)


class NormalizeCaseRefTests(unittest.TestCase):
    def test_latin_homoglyphs_to_cyrillic(self):
        self.assertEqual(normalize_case_ref("A40-308035/2025"),
                         "А40-308035/2025")
        self.assertEqual(normalize_case_ref("2A-1222/2026"),
                         "2А-1222/2026")
        self.assertEqual(normalize_case_ref("C40-1/2025"),
                         "С40-1/2025")

    def test_lowercase_latin(self):
        self.assertEqual(normalize_case_ref("a40-308035/2025"),
                         "А40-308035/2025")

    def test_nbsp_and_extra_spaces(self):
        self.assertEqual(normalize_case_ref("А40-308035/2025"),
                         "А40-308035/2025")
        self.assertEqual(normalize_case_ref("  А40-308035/2025  "),
                         "А40-308035/2025")

    def test_digits_untouched(self):
        self.assertEqual(normalize_case_ref("272-ПЭК25"),
                         "272-ПЭК25")

    def test_already_cyrillic_unchanged(self):
        self.assertEqual(normalize_case_ref("А40-308035/2025"),
                         "А40-308035/2025")

    def test_refs_match(self):
        self.assertTrue(refs_match("A40-308035/2025", "А40-308035/2025"))
        self.assertFalse(refs_match("А40-308035/2025", "А40-999999/2099"))

    def test_import_provider_accepts_latin_ref(self):
        from case_search.kad.import_json import KadImportProvider
        from case_search.kad.parser import fetch_kad_html
        fixture = (REPO_ROOT / "fixtures"
                   / "kad-live-extract-А40-308035-2025.json")
        provider = KadImportProvider(fixture, "A40-308035/2025")
        out = fetch_kad_html(provider, "A40-308035/2025", NOW)
        self.assertEqual(out.status, "ok", out.reason)
        self.assertEqual(out.snapshot.case_number, "А40-308035/2025")


NOW = "2026-09-09T00:00:00Z"

if __name__ == "__main__":
    unittest.main()
