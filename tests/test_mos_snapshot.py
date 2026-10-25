"""mos-gorsud card -> canonical CaseSnapshot tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search import get_mos_case  # noqa: E402
from case_search.errors import MalformedPayloadError  # noqa: E402
from case_search.mos.live import (  # noqa: E402
    KNOWN_COURTS,
    mos_card_to_snapshot,
)
from case_search.schema import CaseSnapshot  # noqa: E402


class MosCardToSnapshotTests(unittest.TestCase):
    def _sample_card(self) -> dict:
        return {
            "court_slug": "horoshevskij",
            "case_number_query": "02-2101/2026",
            "judge": "Черныш В.В.",
            "current_status": "Зарегистрировано, 15.09.2026",
            "parties": [
                {"role": "Истец", "name": "Мачавариани С.З."},
                {"role": "Ответчик", "name": 'АО "АльфаСтрахование"'},
                {"role": "Представитель", "name": "Смирнов А.В."},
            ],
            "events_timeline": [
                {"date": "10.09.2026",
                 "name": "Регистрация поступившего заявления",
                 "published": False, "pdf_url": ""},
                {"date": "15.09.2026",
                 "name": "Заявление принято к производству",
                 "published": True, "pdf_url": ""},
            ],
            "events_timeline_meta": {
                "dropped_duplicates": 0,
                "warnings": [],
            },
            "card_url": "https://mos-gorsud.ru/rs/horoshevskij/"
                        "services/cases/civil/details/"
                        "adf460a1-b12b-11f1-b669-2f14c19231b4",
        }

    def test_builds_case_snapshot(self):
        card = self._sample_card()
        snapshot = mos_card_to_snapshot(card)

        self.assertIsInstance(snapshot, CaseSnapshot)
        self.assertEqual(snapshot.case_number, "02-2101/2026")
        self.assertEqual(
            snapshot.court,
            KNOWN_COURTS["horoshevskij"] + " (mos-gorsud)")
        self.assertEqual(snapshot.judge, "Черныш В.В.")
        self.assertEqual(snapshot.status, "Зарегистрировано, 15.09.2026")

        self.assertEqual(snapshot.parties.plaintiff, ("Мачавариани С.З.",))
        self.assertEqual(
            snapshot.parties.respondent,
            ('АО "АльфаСтрахование"',))
        self.assertEqual(snapshot.parties.third_parties, ("Смирнов А.В.",))

        self.assertEqual(len(snapshot.events_timeline), 2)
        self.assertEqual(snapshot.events_timeline[0].date, "10.09.2026")
        self.assertEqual(
            snapshot.events_timeline[0].name,
            "Регистрация поступившего заявления")
        self.assertFalse(snapshot.events_timeline[0].published)
        self.assertEqual(snapshot.events_timeline[1].date, "15.09.2026")
        self.assertTrue(snapshot.events_timeline[1].published)

        self.assertIsNotNone(snapshot.last_event)
        self.assertEqual(snapshot.last_event.date, "10.09.2026")
        self.assertEqual(
            snapshot.last_event.event_type,
            "Регистрация поступившего заявления")
        self.assertIsNone(snapshot.next_hearing)

        self.assertEqual(snapshot.provenance.source, "mos-gorsud")
        self.assertTrue(snapshot.provenance.source_url)
        self.assertTrue(snapshot.provenance.raw_artifact_hash)
        self.assertTrue(snapshot.updated_at)

    def test_missing_case_number_query_raises(self):
        card = self._sample_card()
        del card["case_number_query"]
        with self.assertRaises(MalformedPayloadError) as ctx:
            mos_card_to_snapshot(card)
        self.assertIn("case_number_query", str(ctx.exception))

    def test_missing_court_slug_raises(self):
        card = self._sample_card()
        del card["court_slug"]
        with self.assertRaises(MalformedPayloadError) as ctx:
            mos_card_to_snapshot(card)
        self.assertIn("court_slug", str(ctx.exception))

    def test_unknown_court_slug_raises(self):
        card = self._sample_card()
        card["court_slug"] = "nonexistent"
        with self.assertRaises(MalformedPayloadError) as ctx:
            mos_card_to_snapshot(card)
        self.assertIn("unknown court_slug", str(ctx.exception))


class PublicApiMosSnapshotTests(unittest.TestCase):
    def test_get_mos_case_returns_snapshot(self):
        raw = {
            "court_slug": "horoshevskij",
            "case_number_query": "02-2101/2026",
            "judge": "Черныш В.В.",
            "current_status": "Зарегистрировано",
            "parties": [{"role": "Истец", "name": "Иванов И.И."}],
            "events_timeline": [
                {"date": "15.09.2026", "name": "Дело зарегистрировано",
                 "published": False, "pdf_url": ""},
            ],
            "events_timeline_meta": {"dropped_duplicates": 0,
                                     "warnings": []},
            "card_url": "https://mos-gorsud.ru/rs/horoshevskij/"
                        "services/cases/civil/details/uuid",
        }

        def fake_get_mos_case(court_slug, case_number, *, kind=None,
                              proxy=None):
            self.assertEqual(court_slug, "horoshevskij")
            self.assertEqual(case_number, "02-2101/2026")
            return raw

        with patch("case_search.config.get_proxy",
                   return_value=None), \
             patch("case_search.mos.live.get_mos_case",
                   side_effect=fake_get_mos_case):
            outcome = get_mos_case("horoshevskij", "02-2101/2026")

        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(outcome.snapshot)
        self.assertIsInstance(outcome.snapshot, CaseSnapshot)
        self.assertEqual(outcome.snapshot.case_number, "02-2101/2026")
        self.assertIn("events_timeline", outcome.extensions)


if __name__ == "__main__":
    unittest.main()
