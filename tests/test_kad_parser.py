"""KAD parser tests: regex fix, fail-closed paths, pipeline outcome mapping."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.kad import parser  # noqa: E402


class RawStub:
    def __init__(self, source, source_url, body, metadata):
        self.source = source
        self.source_url = source_url
        self.media_type = "text/html"
        self.body = body
        self.metadata = metadata


def _kad_data(case_number="А40-308035/2025", **overrides):
    data = {
        "case_number": case_number,
        "status": "Рассматривается в первой инстанции",
        "court": "АС города Москвы",
        "judge": "Шуваева М. В.",
        "plaintiffs": ["POD Trading LLC"],
        "respondents": ["Interactive Brokers LLC"],
        "third_parties": ["ПАО СБЕРБАНК РОССИИ"],
        "other": ["ПОД ТРЕЙДИНГ ЛЛС"],
        "next_hearing": "21.10.2026, 15:00 , Зал судебных заседаний № 6023",
        "duration": "9 месяцев 29 дней",
        "decision_date": "11 ноября 2025",
        "instance_id": "c1a0221b",
        "full_text": "обычный текст",
    }
    data.update(overrides)
    return data


class FakeProvider:
    source = "kad"

    def __init__(self, data=None, metadata=None, media_type="text/html"):
        self._data = data if data is not None else _kad_data()
        self._metadata = metadata
        self._media_type = media_type

    def lookup(self, case_ref):
        meta = {"kad_data": self._data} if self._metadata is None \
            else self._metadata
        return RawStub("kad", f"https://kad.arbitr.ru/Card/x/{case_ref}",
                       "<html>raw card</html>", meta)


class CaseNumberRegexTests(unittest.TestCase):
    """Regression: real KAD numbers are letter+digits before the dash."""

    def test_cyrillic_letter_digits(self):
        self.assertTrue(parser.CASE_NUM_RE.search("А40-308035/2025"))

    def test_variants(self):
        for num in ("А56-1234/2024", "F03-1234/2023", "1А42-999/2025",
                    "А40-1/2025"):
            self.assertTrue(parser.CASE_NUM_RE.search(num), num)

    def test_garbage_rejected(self):
        for num in ("banana", "", "2025/А40", "А40-308035"):
            self.assertFalse(parser.CASE_NUM_RE.fullmatch(num), num)


class ParseKadDataTests(unittest.TestCase):
    def test_valid_sample_parses(self):
        rec = parser.parse_kad_data(
            _kad_data(), source="kad", source_url="u",
            retrieved_at="2026-09-09T00:00:00Z", raw_artifact_hash="h")
        self.assertEqual(rec.case_number, "А40-308035/2025")
        self.assertEqual(rec.plaintiffs, ("POD Trading LLC",))

    def test_snapshot_has_normalized_hearing(self):
        rec = parser.parse_kad_data(
            _kad_data(), source="kad", source_url="u",
            retrieved_at="2026-09-09T00:00:00Z", raw_artifact_hash="h")
        snap = rec.to_case_snapshot()
        self.assertEqual(snap.next_hearing.date, "2026-10-21")
        self.assertEqual(snap.next_hearing.time, "15:00")
        self.assertIn("6023", snap.next_hearing.hall)

    def test_invalid_case_number_fails_closed(self):
        from case_search.errors import MalformedPayloadError
        with self.assertRaises(MalformedPayloadError):
            parser.parse_kad_data(
                _kad_data(case_number="banana"), source="kad",
                source_url="u", retrieved_at="n", raw_artifact_hash="h")

    def test_missing_status_fails_closed(self):
        from case_search.errors import MalformedPayloadError
        with self.assertRaises(MalformedPayloadError):
            parser.parse_kad_data(
                _kad_data(status=""), source="kad", source_url="u",
                retrieved_at="n", raw_artifact_hash="h")

    def test_missing_court_fails_closed(self):
        from case_search.errors import MalformedPayloadError
        with self.assertRaises(MalformedPayloadError):
            parser.parse_kad_data(
                _kad_data(court=""), source="kad", source_url="u",
                retrieved_at="n", raw_artifact_hash="h")

    def test_waf_text_raises_waf_detected(self):
        from case_search.errors import WafDetectedError
        with self.assertRaises(WafDetectedError):
            parser.parse_kad_data(
                _kad_data(full_text="Доступ заблокирован, пройдите captcha"),
                source="kad", source_url="u", retrieved_at="n",
                raw_artifact_hash="h")

    def test_missing_judge_becomes_warning_not_error(self):
        rec = parser.parse_kad_data(
            _kad_data(judge=""), source="kad", source_url="u",
            retrieved_at="n", raw_artifact_hash="h")
        self.assertTrue(any("judge" in w for w in rec.warnings))


class FetchKadHtmlPipelineTests(unittest.TestCase):
    NOW = "2026-09-09T00:00:00Z"
    CASE = "А40-308035/2025"

    def test_ok_path_returns_normalized_snapshot(self):
        out = parser.fetch_kad_html(FakeProvider(), self.CASE, self.NOW)
        self.assertEqual(out.status, "ok")
        self.assertEqual(out.reason, "normalized")
        self.assertEqual(out.snapshot.case_number, self.CASE)
        self.assertEqual(len(out.snapshot.provenance.raw_artifact_hash), 64)
        self.assertEqual(out.snapshot.provenance.parser_version,
                         parser.KAD_PARSER_VERSION)

    def test_missing_kad_data_is_error_not_snapshot(self):
        out = parser.fetch_kad_html(
            FakeProvider(metadata={}), self.CASE, self.NOW)
        self.assertEqual(out.status, "error")
        self.assertIsNone(out.snapshot)
        self.assertIn("no KAD extracted data", out.reason)

    def test_waf_is_degraded_never_snapshot(self):
        out = parser.fetch_kad_html(
            FakeProvider(_kad_data(full_text="Подтверждение действия")),
            self.CASE, self.NOW)
        self.assertEqual(out.status, "degraded")
        self.assertIsNone(out.snapshot)
        self.assertIn("captcha_or_waf", out.reason)

    def test_identity_mismatch_is_error(self):
        out = parser.fetch_kad_html(
            FakeProvider(_kad_data(case_number="А40-111/2025")),
            self.CASE, self.NOW)
        self.assertEqual(out.status, "error")
        self.assertIsNone(out.snapshot)
        self.assertIn("identity mismatch", out.reason)

    def test_non_html_media_type_is_error(self):
        provider = FakeProvider()
        orig = provider.lookup

        def lookup(case_ref):
            raw = orig(case_ref)
            raw.media_type = "application/json"
            return raw
        provider.lookup = lookup
        out = parser.fetch_kad_html(provider, self.CASE, self.NOW)
        self.assertEqual(out.status, "error")
        self.assertIn("text/html", out.reason)

    def test_hearing_without_hall_is_error_fail_closed(self):
        out = parser.fetch_kad_html(
            FakeProvider(_kad_data(next_hearing="01.12.2026, 10:00")),
            self.CASE, self.NOW)
        self.assertEqual(out.status, "error")
        self.assertIsNone(out.snapshot)
        self.assertIn("hall", out.reason)



class ExtractExpressionTests(unittest.TestCase):
    """EXTRACT_EXPRESSION must contain the live-verified selectors
    (regression: old selectors matched nothing on the real card)."""

    def test_contains_live_verified_selectors(self):
        expr = parser.EXTRACT_EXPRESSION
        for marker in (
            "js-case-header-case_num",
            "b-case-header-desc",
            "table.b-case-info td.plaintiffs",
            "table.b-case-info td.defendants li",
            "table.b-case-info td.third li",
            "table.b-case-info td.others li",
            "td.arbitration a",
            "b-instanceAdditional",
            "js-instanceId",
        ):
            self.assertIn(marker, expr, marker)

    def test_no_stale_selectors(self):
        expr = parser.EXTRACT_EXPRESSION
        for stale in ("b-case-parties-plaintiffs",
                      "h4.b-case-instance"):
            self.assertNotIn(stale, expr, stale)

    def test_valid_javascript_braces(self):
        # crude balance check: the JSON.stringify block must close
        expr = parser.EXTRACT_EXPRESSION
        self.assertEqual(expr.count("{"), expr.count("}"))
        self.assertEqual(expr.count("("), expr.count(")"))

if __name__ == "__main__":
    unittest.main()
