"""SudAct import parser tests: 4 live fixtures + fail-closed paths."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import MalformedPayloadError  # noqa: E402
from case_search.sudact.parser import (SUDACT_PARSER_VERSION,  # noqa: E402
                                         SudactImportProvider,
                                         detect_challenge,
                                         fetch_sudact_html,
                                         parse_sudact_html)

FIX = REPO_ROOT / "fixtures" / "sudact"
NOW = "2026-09-09T00:00:00Z"

LIVE_CASES = [
    ("regular-doc-2A-3400-2025.html", "2А-3400/2025",
     "Минусинский городской суд", "Шкарин Д.В"),
    ("regular-doc-2A-1222-2026.html", "2А-1222/2026",
     "Новосибирский  районный суд", "Барбалюк М.В"),
    ("arbitral-doc-A41-69570-2025.html", "А41-69570/2025",
     "Арбитражный суд Московской области", "Петрова Ю.А"),
    ("arbitral-doc-asmo-2026.html", "А40-273535/2025",
     "Арбитражный суд города Москвы", "Пронин А.П"),
]


class LiveFixtureTests(unittest.TestCase):
    """All four live-verified documents must parse end-to-end."""

    def test_all_live_documents_parse(self):
        for fname, case_ref, court, judge in LIVE_CASES:
            with self.subTest(case=case_ref):
                out = fetch_sudact_html(
                    SudactImportProvider(FIX / fname, case_ref),
                    case_ref, NOW)
                self.assertEqual(out.status, "ok", out.reason)
                s = out.snapshot
                self.assertEqual(s.case_number, case_ref)
                self.assertEqual(s.court, court)
                self.assertTrue(s.judge.startswith(judge))
                self.assertEqual(len(s.provenance.raw_artifact_hash), 64)
                self.assertEqual(s.provenance.parser_version,
                                 SUDACT_PARSER_VERSION)

    def test_truncated_year_gets_warning(self):
        out = fetch_sudact_html(
            SudactImportProvider(FIX / "arbitral-doc-asmo-2026.html",
                                 "А40-273535/2025"),
            "А40-273535/2025", NOW)
        self.assertEqual(out.status, "ok")
        record = parse_sudact_html(
            (FIX / "arbitral-doc-asmo-2026.html").read_text(
                encoding="utf-8", errors="replace"),
            case_ref="А40-273535/2025", source="s", source_url="u",
            retrieved_at=NOW, raw_artifact_hash="h")
        self.assertTrue(any("truncated" in w for w in record.warnings))


class FailClosedTests(unittest.TestCase):
    def _parse(self, html: str, case_ref: str = "А40-1/2025"):
        return parse_sudact_html(html, case_ref=case_ref, source="s",
                                 source_url="u", retrieved_at=NOW,
                                 raw_artifact_hash="h")

    def test_empty_payload_raises(self):
        from case_search.errors import EmptyPayloadError
        with self.assertRaises(EmptyPayloadError):
            self._parse("   ")

    def test_challenge_text_raises_waf(self):
        from case_search.errors import WafDetectedError
        html = ('<html><h1>x</h1><div class="h-col1-inner3">'
                "Подтвердите, что вы не робот captcha</div></html>")
        with self.assertRaises(WafDetectedError):
            self._parse(html)

    def test_no_container_raises(self):
        with self.assertRaises(MalformedPayloadError):
            self._parse("<html><h1>doc</h1><p>text</p></html>")

    def test_no_h1_raises(self):
        html = ('<html><div class="h-col1-inner3">'
                "<p>Суд Х (Регион)</p></div></html>")
        with self.assertRaises(MalformedPayloadError):
            self._parse(html)

    def test_no_court_raises(self):
        html = ('<html><h1>t</h1><div class="h-col1-inner3">'
                "<p>без скобок</p></div></html>")
        with self.assertRaises(MalformedPayloadError):
            self._parse(html)

    def test_foreign_case_ref_raises(self):
        html = ('<html><h1>Решение по делу № А40-999/2025</h1>'
                '<div class="h-col1-inner3"><p>Суд Х (Регион)</p>'
                "<p>дело А40-999/2025 решено</p></div></html>")
        with self.assertRaises(MalformedPayloadError) as ctx:
            self._parse(html, case_ref="А40-888/2024")
        self.assertIn("identity mismatch", str(ctx.exception))

    def test_truncated_number_with_future_year_rejected(self):
        # page shows '/20' but document date year (2024) is LESS than
        # the claimed case year (2025) -> inconsistent, must reject
        html = ('<html><h1>Решение</h1><div class="h-col1-inner3">'
                "<p>Суд Х (Регион)</p><p>1 января 2024 г. Дело № "
                "А40-1/20</p></div></html>")
        with self.assertRaises(MalformedPayloadError):
            self._parse(html, case_ref="А40-1/2025")

    def test_import_provider_staged_ref_mismatch(self):
        from case_search.errors import SourceUnavailableError
        provider = SudactImportProvider(FIX / "regular-doc-2A-3400-2025.html",
                                        "2А-3400/2025")
        with self.assertRaises(SourceUnavailableError):
            provider.lookup("2А-9999/2025")

    def test_import_provider_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = SudactImportProvider(Path(tmp) / "nope.html",
                                            "2А-3400/2025")
            out = fetch_sudact_html(provider, "2А-3400/2025", NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("missing", out.reason)

    def test_non_html_media_type_is_error(self):
        class JsonProvider:
            name = "sudact-import"

            def lookup(self, case_ref):
                from case_search.sources.base import RawSourceResult
                return RawSourceResult("s", "u", 200, "{}",
                                       media_type="application/json")

        out = fetch_sudact_html(JsonProvider(), "2А-3400/2025", NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("text/html", out.reason)


class JudgeExtractionTests(unittest.TestCase):
    def test_judge_from_meta(self):
        record = parse_sudact_html(
            (FIX / "regular-doc-2A-3400-2025.html").read_text(
                encoding="utf-8", errors="replace"),
            case_ref="2А-3400/2025", source="s", source_url="u",
            retrieved_at=NOW, raw_artifact_hash="h")
        self.assertEqual(record.judge, "Шкарин Д.В")

    def test_no_judge_is_warning_not_error(self):
        html = ('<html><h1>Решение по делу № А40-1/2025</h1>'
                '<div class="h-col1-inner3"><p>Суд Х (Регион)</p>'
                "<p>дело А40-1/2025</p></div></html>")
        record = self._parse_ok(html)
        self.assertEqual(record.judge, "")
        self.assertTrue(any("judge" in w for w in record.warnings))

    def _parse_ok(self, html):
        return parse_sudact_html(html, case_ref="А40-1/2025", source="s",
                                 source_url="u", retrieved_at=NOW,
                                 raw_artifact_hash="h")


if __name__ == "__main__":
    unittest.main()
