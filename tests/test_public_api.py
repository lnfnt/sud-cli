"""Public Python API tests (colleague-legal feedback: no subprocess)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search import (ApiOutcome, import_document,  # noqa: E402
                           lookup_case, search_practice)

SAVED = REPO_ROOT / "fixtures" / "kad-saved-card-А40-308035-2025.html"
EXTRACT = REPO_ROOT / "fixtures" / "kad-live-extract-А40-308035-2025.json"


class ImportDocumentTests(unittest.TestCase):
    def test_kad_html_with_latin_ref(self):
        out = import_document(SAVED, "A40-308035/2025")
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.source, "kad-html")
        self.assertEqual(out.snapshot.case_number, "А40-308035/2025")
        self.assertEqual(out.snapshot.judge, "Шуваева М. В.")
        self.assertEqual(len(out.snapshot.events_timeline), 12)
        self.assertIn("instances", out.extensions)

    def test_sudact_import(self):
        out = import_document(
            REPO_ROOT / "fixtures" / "sudact"
            / "arbitral-doc-asmo-2026.html", "А40-273535/2025")
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.source, "sudact")

    def test_vsrf_import(self):
        out = import_document(
            REPO_ROOT / "fixtures" / "vsrf"
            / "presidium-272-PEK25-A40-217461-2024.pdf", "272-ПЭК25")
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.source, "vsrf")

    def test_unknown_file_is_error_outcome(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.txt"
            junk.write_text("nothing", encoding="utf-8")
            out = import_document(junk, "А40-1/2025")
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "error")

    def test_envelope_to_dict(self):
        out = import_document(SAVED, "А40-308035/2025")
        d = out.to_dict()
        self.assertEqual(d["status"], "ok")
        self.assertIsNotNone(d["snapshot"])
        self.assertIn("extensions", d)


class LookupCaseTests(unittest.TestCase):
    def test_with_fake_browser(self):
        data = json.loads(EXTRACT.read_text(encoding="utf-8"))
        if isinstance(data, str):
            data = json.loads(data)
        # вырезать пустые поля, которых нет в живом extract v2
        class FakeBrowser:
            def __call__(self, url, timeout_ms):
                class P:
                    kad_data = data
                return P()
        out = lookup_case(
            "A40-308035/2025", browser=FakeBrowser(),
            card_url="https://kad.arbitr.ru/Card/x")
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.snapshot.case_number, "А40-308035/2025")

    def test_identity_mismatch_is_error(self):
        data = json.loads(EXTRACT.read_text(encoding="utf-8"))
        if isinstance(data, str):
            data = json.loads(data)

        class FakeBrowser:
            def __call__(self, url, timeout_ms):
                class P:
                    kad_data = data
                return P()
        out = lookup_case(
            "А40-999999/2099", browser=FakeBrowser(),
            card_url="https://kad.arbitr.ru/Card/x")
        self.assertEqual(out.status, "error")
        self.assertIn("mismatch", out.reason)

    def test_browser_without_data_is_error(self):
        class BadBrowser:
            def __call__(self, url, timeout_ms):
                class P:
                    html = "<html/>"
                return P()
        out = lookup_case("А40-1/2025", browser=BadBrowser(),
                          card_url="u")
        self.assertEqual(out.status, "error")
        self.assertIn("EXTRACT_EXPRESSION", out.reason)


class SearchPracticeTests(unittest.TestCase):
    def test_with_fake_fetcher(self):
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-arbitral.html").read_text(
                    encoding="utf-8", errors="replace")

        def fetcher(url):
            return html
        out = search_practice("банкротство", fetcher=fetcher)
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(len(out.extensions["results"]), 10)
        self.assertEqual(out.extensions["next_page"], 2)

    def test_garbage_query_is_error(self):
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-arbitral.html").read_text(
                    encoding="utf-8", errors="replace")

        def fetcher(url):
            return html
        out = search_practice("zzqxvбредовыйзапрос12345xyz",
                              fetcher=fetcher)
        self.assertEqual(out.status, "error")
        self.assertIn("default list", out.reason)


class EnvelopeTests(unittest.TestCase):
    def test_ok_property_and_error_shape(self):
        o = ApiOutcome(status="error", reason="x")
        self.assertFalse(o.ok)
        d = o.to_dict()
        self.assertIsNone(d["snapshot"])
        self.assertNotIn("extensions", d)


if __name__ == "__main__":
    unittest.main()
