"""VS RF PDF import tests: live presidium PDF + fail-closed paths."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (MalformedPayloadError,  # noqa: E402
                                  SourceUnavailableError)
from case_search.vsrf.parser import (VSRF_PARSER_VERSION,  # noqa: E402
                                       VsrfImportProvider, detect_challenge,
                                       extract_pdf_text, fetch_vsrf_pdf,
                                       parse_vsrf_pdf_text)

FIX = REPO_ROOT / "fixtures" / "vsrf"
PLEDIUM = FIX / "presidium-272-PEK25-A40-217461-2024.pdf"
NOW = "2026-09-09T00:00:00Z"


class LivePdfTests(unittest.TestCase):
    def test_presidium_pdf_by_vs_case_number(self):
        out = fetch_vsrf_pdf(VsrfImportProvider(PLEDIUM, "272-ПЭК25"),
                             "272-ПЭК25", NOW)
        self.assertEqual(out.status, "ok", out.reason)
        s = out.snapshot
        self.assertEqual(s.case_number, "272-ПЭК25")
        self.assertIn("Президиума", s.court)
        self.assertEqual(s.judge, "Краснова И.В.")
        self.assertEqual(len(s.provenance.raw_artifact_hash), 64)
        self.assertEqual(s.provenance.parser_version, VSRF_PARSER_VERSION)

    def test_presidium_pdf_by_lower_court_number(self):
        """The requested ref may be the lower-court arbitration case
        reviewed by the presidium."""
        out = fetch_vsrf_pdf(VsrfImportProvider(PLEDIUM, "А40-217461/2024"),
                             "А40-217461/2024", NOW)
        self.assertEqual(out.status, "ok", out.reason)

    def test_extract_pdf_text_nonempty(self):
        text = extract_pdf_text(PLEDIUM)
        self.assertIn("ПОСТАНОВЛЕНИЕ", text)
        self.assertIn("272-ПЭК25", text)


class FailClosedTests(unittest.TestCase):
    def _parse(self, text: str, case_ref: str = "272-ПЭК25"):
        return parse_vsrf_pdf_text(text, case_ref=case_ref, source="s",
                                   source_url="u", retrieved_at=NOW,
                                   raw_artifact_hash="h")

    def test_empty_text_raises(self):
        from case_search.errors import EmptyPayloadError
        with self.assertRaises(EmptyPayloadError):
            self._parse("   ")

    def test_challenge_text_raises_waf(self):
        from case_search.errors import WafDetectedError
        with self.assertRaises(WafDetectedError):
            self._parse("ПОСТАНОВЛЕНИЕ Президиума captcha Дело № 272-ПЭК25")

    def test_no_doc_kind_raises(self):
        with self.assertRaises(MalformedPayloadError):
            self._parse("случайный текст без маркера документа")

    def test_no_court_marker_raises(self):
        with self.assertRaises(MalformedPayloadError):
            self._parse("ПОСТАНОВЛЕНИЕ какой-то организации Дело № 272-ПЭК25")

    def test_foreign_case_ref_raises(self):
        text = ("ПОСТАНОВЛЕНИЕ ПРЕЗИДИУМА ВЕРХОВНОГО СУДА\n"
                "Дело № 111-ПЭК25\n1 июля 2026 г.\nпо делу № А40-1/2024")
        with self.assertRaises(MalformedPayloadError) as ctx:
            self._parse(text, case_ref="А40-999999/2099")
        self.assertIn("identity mismatch", str(ctx.exception))

    def test_provider_staged_ref_mismatch(self):
        provider = VsrfImportProvider(PLEDIUM, "272-ПЭК25")
        with self.assertRaises(SourceUnavailableError):
            provider.lookup("111-ПЭК25")

    def test_provider_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = VsrfImportProvider(Path(tmp) / "no.pdf", "272-ПЭК25")
            out = fetch_vsrf_pdf(provider, "272-ПЭК25", NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("missing", out.reason)

    def test_non_pdf_media_type_is_error(self):
        class JsonProvider:
            name = "vsrf-import"

            def lookup(self, case_ref):
                from case_search.sources.base import RawSourceResult
                return RawSourceResult("s", "u", 200, "",
                                       media_type="application/json")

        out = fetch_vsrf_pdf(JsonProvider(), "272-ПЭК25", NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("application/pdf", out.reason)

    def test_corrupt_pdf_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.pdf"
            bad.write_bytes(b"%PDF-1.4 broken")
            out = fetch_vsrf_pdf(VsrfImportProvider(bad, "272-ПЭК25"),
                                 "272-ПЭК25", NOW)
        self.assertEqual(out.status, "error")

    def test_detect_challenge(self):
        self.assertTrue(detect_challenge("пройдите captcha"))
        self.assertFalse(detect_challenge("обычный текст"))


if __name__ == "__main__":
    unittest.main()
