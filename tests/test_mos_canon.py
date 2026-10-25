"""mos-gorsud motion -> events_timeline canonization tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.mos.live import (  # noqa: E402
    MosLiveError,
    canonize_mos_events,
    get_mos_case,
)


class CanonizeEventsTests(unittest.TestCase):
    def test_typical_row_without_document(self):
        result = canonize_mos_events([
            {"date": "10.09.2026", "event": "Регистрация поступившего заявления", "document": ""},
        ])
        timeline = result["events_timeline"]
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["date"], "10.09.2026")
        self.assertEqual(timeline[0]["name"], "Регистрация поступившего заявления")
        self.assertFalse(timeline[0]["published"])
        self.assertEqual(timeline[0]["pdf_url"], "")
        self.assertEqual(result["dropped_duplicates"], 0)
        self.assertEqual(result["warnings"], [])

    def test_row_with_document_is_published(self):
        result = canonize_mos_events([
            {"date": "15.09.2026", "event": "Заявление принято к производству", "document": "Определение"},
        ])
        timeline = result["events_timeline"]
        self.assertTrue(timeline[0]["published"])
        self.assertEqual(timeline[0]["pdf_url"], "")

    def test_publication_marker_without_document(self):
        result = canonize_mos_events([
            {"date": "16.09.2026", "event": "Определение изготовлено", "document": ""},
        ])
        timeline = result["events_timeline"]
        self.assertTrue(timeline[0]["published"])

    def test_whitespace_normalization(self):
        result = canonize_mos_events([
            {"date": "10.09.2026", "event": "  Регистрация   поступившего  заявления\n", "document": ""},
        ])
        self.assertEqual(
            result["events_timeline"][0]["name"],
            "Регистрация поступившего заявления",
        )

    def test_pdf_url_extraction(self):
        result = canonize_mos_events([
            {"date": "17.09.2026", "event": "Документ", "document": "https://mos-gorsud.ru/cases/docs/content/foo.pdf"},
            {"date": "18.09.2026", "event": "Документ 2", "document": "/cases/docs/content/bar.pdf?x=1"},
            {"date": "19.09.2026", "event": "Документ 3", "document": "baz.pdf"},
        ])
        timeline = result["events_timeline"]
        self.assertEqual(timeline[0]["pdf_url"], "https://mos-gorsud.ru/cases/docs/content/foo.pdf")
        self.assertEqual(timeline[1]["pdf_url"], "https://mos-gorsud.ru/cases/docs/content/bar.pdf?x=1")
        self.assertEqual(timeline[2]["pdf_url"], "https://mos-gorsud.ru/baz.pdf")

    def test_duplicate_keeps_first(self):
        result = canonize_mos_events([
            {"date": "10.09.2026", "event": "Регистрация", "document": ""},
            {"date": "10.09.2026", "event": "Регистрация", "document": "Определение"},
            {"date": "11.09.2026", "event": "Заседание", "document": ""},
        ])
        timeline = result["events_timeline"]
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["date"], "10.09.2026")
        self.assertEqual(timeline[0]["name"], "Регистрация")
        self.assertFalse(timeline[0]["published"])
        self.assertEqual(result["dropped_duplicates"], 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("duplicate", result["warnings"][0])

    def test_missing_date_raises(self):
        with self.assertRaises(MosLiveError):
            canonize_mos_events([{"event": "Регистрация", "document": ""}])

    def test_missing_event_raises(self):
        with self.assertRaises(MosLiveError):
            canonize_mos_events([{"date": "10.09.2026", "document": ""}])

    def test_empty_event_raises(self):
        with self.assertRaises(MosLiveError):
            canonize_mos_events([{"date": "10.09.2026", "event": "   ", "document": ""}])


class GetMosCaseTimelineTests(unittest.TestCase):
    def test_get_mos_case_includes_events_timeline(self):
        search_html = (
            '<a href="/rs/horoshevskij/services/cases/civil/details/'
            'adf460a1-b12b-11f1-b669-2f14c19231b4">x</a>'
        )
        card_html = """\
<html><body>
<div class="row"><div class="left">Cудья</div>
<div class="right">Черныш В.В.</div></div>
<table><tr><th>История состояний</h3></tr></table><table><tbody>
<tr><td>10.09.2026</td><td>Регистрация поступившего заявления</td><td></td></tr>
<tr><td>15.09.2026</td><td>Заявление принято к производству</td><td>Определение</td></tr>
</tbody></table>
</body></html>
"""

        def fake_http_get(url, *, proxy=None, timeout=30):
            if "/search?" in url:
                return search_html
            return card_html

        with patch("case_search.mos.live._http_get", fake_http_get):
            result = get_mos_case("horoshevskij", "02-2101/2026")

        self.assertIn("events_timeline", result)
        self.assertIn("events_timeline_meta", result)
        self.assertEqual(len(result["events_timeline"]), 2)
        self.assertEqual(result["events_timeline"][0]["date"], "10.09.2026")
        self.assertEqual(result["events_timeline"][0]["published"], False)
        self.assertEqual(result["events_timeline"][1]["published"], True)
        self.assertIn("motion", result)
        self.assertEqual(len(result["motion"]), 2)


if __name__ == "__main__":
    unittest.main()
