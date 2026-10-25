"""KAD parser hardening against instance-tab variations.

Synthetic HTML fixtures under fixtures/kad/variations/ mimic appeal /
cassation / supervision tab structures.  These tests verify that the
parser detects the instance level, extracts case numbers, and keeps
fail-closed validation intact without network or browser.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.kad.html_import import (  # noqa: E402
    KadHtmlImportProvider, fetch_kad_saved_html, parse_kad_saved_html)
from case_search.kad.parser import parse_kad_data  # noqa: E402

VARIATIONS = REPO_ROOT / "fixtures" / "kad" / "variations"
NOW = "2026-09-17T00:00:00Z"


class VariationFixtureTests(unittest.TestCase):
    """Each synthetic fixture parses and reports the correct instance."""

    def _load(self, name: str, case_ref: str):
        html = (VARIATIONS / name).read_text(encoding="utf-8")
        data = parse_kad_saved_html(
            html, case_ref=case_ref, source="kad-variation",
            source_url=f"file://{VARIATIONS / name}", retrieved_at=NOW,
            raw_artifact_hash="h")
        record = parse_kad_data(
            data, source="kad-variation",
            source_url=f"file://{VARIATIONS / name}", retrieved_at=NOW,
            raw_artifact_hash="h")
        return data, record

    def test_appeal_variation(self):
        data, record = self._load(
            "kad-variation-appeal-16AP-1234-2026.html", "16АП-1234/2026")
        self.assertEqual(record.case_number, "16АП-1234/2026")
        self.assertEqual(record.instance_level, "appeal")
        self.assertEqual(record.court, "16 Арбитражный апелляционный суд")
        self.assertTrue(record.plaintiffs)
        self.assertTrue(record.events)
        self.assertTrue(record.instances)
        self.assertGreater(len(record.events), 0)
        self.assertGreater(len(record.instances), 0)

    def test_cassation_variation(self):
        ref = "02-3696/2026(02-1234/2025)"
        data, record = self._load(
            "kad-variation-cassation-02-3696-2026.html", ref)
        self.assertEqual(record.case_number, ref)
        self.assertEqual(record.instance_level, "cassation")
        self.assertEqual(record.court, "Арбитражный суд Волго-Вятского округа")
        self.assertTrue(record.plaintiffs)
        self.assertTrue(record.events)
        self.assertTrue(record.instances)
        self.assertGreater(len(record.events), 0)
        self.assertGreater(len(record.instances), 0)

    def test_supervision_variation(self):
        data, record = self._load(
            "kad-variation-supervision-VAS-12345-2026.html", "ВАС-12345/2026")
        self.assertEqual(record.case_number, "ВАС-12345/2026")
        self.assertEqual(record.instance_level, "supervision")
        self.assertEqual(
            record.court, "Высший Арбитражный Суд Российской Федерации")
        self.assertTrue(record.plaintiffs)
        self.assertTrue(record.events)
        self.assertTrue(record.instances)
        self.assertGreater(len(record.events), 0)
        self.assertGreater(len(record.instances), 0)

    def test_variation_pipeline_returns_snapshot(self):
        """The full fetch_kad_saved_html pipeline works for each variation."""
        for name, ref in (
            ("kad-variation-appeal-16AP-1234-2026.html", "16АП-1234/2026"),
            ("kad-variation-cassation-02-3696-2026.html",
             "02-3696/2026(02-1234/2025)"),
            ("kad-variation-supervision-VAS-12345-2026.html",
             "ВАС-12345/2026"),
        ):
            with self.subTest(name=name):
                out = fetch_kad_saved_html(
                    KadHtmlImportProvider(VARIATIONS / name, ref), ref, NOW)
                self.assertEqual(out.status, "ok", out.reason)
                self.assertIsNotNone(out.snapshot)
                self.assertEqual(out.snapshot.case_number, ref)


class InstanceDetectionTests(unittest.TestCase):
    """Unit-level checks for instance detection from number + context."""

    def test_first_instance_by_status(self):
        record = parse_kad_data(
            {
                "case_number": "А40-308035/2025",
                "status": "Рассматривается в первой инстанции",
                "court": "АС города Москвы",
                "full_text": "обычный текст",
            },
            source="t", source_url="u", retrieved_at=NOW,
            raw_artifact_hash="h")
        self.assertEqual(record.instance_level, "first")

    def test_first_instance_number_on_appeal_tab(self):
        """A first-instance number is not mislabelled when context says appeal."""
        record = parse_kad_data(
            {
                "case_number": "А40-308035/2025",
                "status": "Рассмотрено в апелляционной инстанции",
                "court": "16 ААС",
                "full_text": "Апелляционная инстанция",
            },
            source="t", source_url="u", retrieved_at=NOW,
            raw_artifact_hash="h")
        self.assertEqual(record.instance_level, "appeal")

    def test_appeal_number(self):
        record = parse_kad_data(
            {
                "case_number": "16АП-1234/2026",
                "status": "Рассмотрено",
                "court": "16 ААС",
                "full_text": "",
            },
            source="t", source_url="u", retrieved_at=NOW,
            raw_artifact_hash="h")
        self.assertEqual(record.instance_level, "appeal")

    def test_cassation_number(self):
        record = parse_kad_data(
            {
                "case_number": "02-3696/2026(02-1234/2025)",
                "status": "Рассматривается",
                "court": "ФАС ВВО",
                "full_text": "",
            },
            source="t", source_url="u", retrieved_at=NOW,
            raw_artifact_hash="h")
        self.assertEqual(record.instance_level, "cassation")

    def test_supervision_number(self):
        record = parse_kad_data(
            {
                "case_number": "ВАС-12345/2026",
                "status": "Рассмотрено",
                "court": "ВАС РФ",
                "full_text": "",
            },
            source="t", source_url="u", retrieved_at=NOW,
            raw_artifact_hash="h")
        self.assertEqual(record.instance_level, "supervision")


class CaseNumberRegexTests(unittest.TestCase):
    """The shared KAD regex recognises instance-specific numbers."""

    def test_appeal_number_matches(self):
        from case_search.kad.parser import CASE_NUM_RE
        self.assertTrue(CASE_NUM_RE.search("16АП-1234/2026"))
        # fullmatch should cover the whole number
        self.assertEqual(
            CASE_NUM_RE.fullmatch("16АП-1234/2026").group(0),
            "16АП-1234/2026")

    def test_cassation_number_matches(self):
        from case_search.kad.parser import CASE_NUM_RE
        self.assertTrue(CASE_NUM_RE.search("02-3696/2026(02-1234/2025)"))
        self.assertEqual(
            CASE_NUM_RE.fullmatch("02-3696/2026(02-1234/2025)").group(0),
            "02-3696/2026(02-1234/2025)")

    def test_supervision_number_matches(self):
        from case_search.kad.parser import CASE_NUM_RE
        self.assertTrue(CASE_NUM_RE.search("ВАС-12345/2026"))
        self.assertEqual(
            CASE_NUM_RE.fullmatch("ВАС-12345/2026").group(0),
            "ВАС-12345/2026")

    def test_first_instance_number_still_matches(self):
        from case_search.kad.parser import CASE_NUM_RE
        self.assertTrue(CASE_NUM_RE.search("А40-308035/2025"))

    def test_garbage_rejected(self):
        from case_search.kad.parser import CASE_NUM_RE
        for bad in ("banana", "", "2025/А40", "А40-308035"):
            self.assertFalse(CASE_NUM_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
