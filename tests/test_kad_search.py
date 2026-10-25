"""KAD UI-search + case history tests (fake browser ops, no network)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.kad.search import (KadSearchError,  # noqa: E402
                                      kad_case_history, search_kad)


class FakeOps:
    """Scripted browser ops: queue of evaluate responses."""

    def __init__(self, eval_results: list[str]):
        self._results = list(eval_results)
        self.navigated: list[str] = []

    def navigate(self, url):
        self.navigated.append(url)

    def evaluate(self, js):
        if not self._results:
            return "[]"
        item = self._results.pop(0)
        return item if isinstance(item, str) else json.dumps(item)

    def wait_seconds(self, s):
        pass


ROW = json.dumps({"rows": [{
    "case_number": "А19-30387/2025",
    "kad_url": "https://kad.arbitr.ru/Card/d4dd1077-6887-4b84-a54b-e5089f4f2e4d",
    "judge": "Пущина Т. Н.",
    "court": "АС Иркутской области Пущина Т. Н.",
    "plaintiff": "ООО Арента",
    "respondent": "ООО ТОБОЛ",
    "date": "15.12.2025",
}], "captcha": False})

CARD = json.dumps({
    "case_number": "04АП-1719/2026",
    "court": "4 арбитражный апелляционный суд",
    "status": "Рассмотрение дела завершено",
    "instances": [
        {"level": "Апелляционная инстанция",
         "court": "4 арбитражный апелляционный суд",
         "instance_id": "i2"},
        {"level": "Первая инстанция",
         "court": "АС Иркутской области", "instance_id": "i1"},
    ],
    "events": [{"date": "15.03.2026", "type": "Определение",
                "subject": "", "result": "к рассмотрению",
                "pdf": "", "published": True}],
})


class SearchKadTests(unittest.TestCase):
    def test_happy_path(self):
        ops = FakeOps(["true", "clicked", json.loads(ROW)])
        res = search_kad("А19-30387/2025", ops)
        self.assertTrue(res.ok)
        self.assertEqual(res.rows[0].case_number, "А19-30387/2025")
        self.assertEqual(res.rows[0].uuid,
                         "d4dd1077-6887-4b84-a54b-e5089f4f2e4d")
        self.assertEqual(len(res.rows[0].to_dict()), 8)

    def test_form_not_ready_raises(self):
        ops = FakeOps(["false"] * 20)
        with self.assertRaises(KadSearchError):
            search_kad("А19-30387/2025", ops)

    def test_captcha_raises(self):
        captcha = json.dumps({"rows": [], "captcha": True})
        ops = FakeOps(["true", "clicked", json.loads(captcha)])
        with self.assertRaises(KadSearchError) as ctx:
            search_kad("А19-30387/2025", ops)
        self.assertIn("captcha", str(ctx.exception))

    def test_no_results_raises(self):
        empty = json.dumps({"rows": [], "captcha": False})
        ops = FakeOps(["true", "clicked"] + [json.loads(empty)] * 10)
        with self.assertRaises(KadSearchError):
            search_kad("А40-000/2030", ops)

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            search_kad("  ", FakeOps([]))


class CaseHistoryTests(unittest.TestCase):
    def test_full_flow(self):
        # queue: form-ready, clicked, ROW, card JSON
        ops = FakeOps(["true", "clicked", json.loads(ROW), "ok", json.loads(CARD)])
        result = kad_case_history("А19-30387/2025", ops)
        self.assertEqual(result["case_number"], "04АП-1719/2026")
        self.assertEqual(result["kad_uuid"],
                         "d4dd1077-6887-4b84-a54b-e5089f4f2e4d")
        self.assertEqual(len(result["instances"]), 2)
        self.assertEqual(len(result["events"]), 1)

    def test_case_not_found_raises(self):
        empty = json.dumps({"rows": [], "captcha": False})
        ops = FakeOps(["true", "clicked"] + [json.loads(empty)] * 10)
        with self.assertRaises(KadSearchError):
            kad_case_history("А40-000/2030", ops)


if __name__ == "__main__":
    unittest.main()
