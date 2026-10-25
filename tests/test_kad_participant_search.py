"""KAD participant search tests (fake browser ops, no network)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search import (ApiOutcome, kad_search as public_kad_search)  # noqa: E402
from case_search.errors import ValidationError  # noqa: E402
from case_search.kad.search import (  # noqa: E402
    KadParticipantResult,
    kad_search,
)


class FakeOps:
    """Scripted browser ops: queue of evaluate responses."""

    def __init__(self, eval_results: list):
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


def _row(case_number: str = "А40-308035/2025") -> str:
    return json.dumps({
        "rows": [{
            "case_number": case_number,
            "kad_url": "https://kad.arbitr.ru/Card/d4dd1077-6887-4b84-a54b-e5089f4f2e4d",
            "judge": "Шуваева М. В.",
            "court": "АС Москвы",
            "plaintiff": "ООO Истец",
            "respondent": "ООO Ответчик",
            "date": "15.12.2025",
        }],
        "captcha": False,
        "waf": False,
    })


CLICKED = json.dumps({"status": "clicked"})
CAPTCHA = json.dumps({"rows": [], "captcha": True, "waf": False})
WAF = json.dumps({"rows": [], "captcha": False, "waf": True})
EMPTY = json.dumps({"rows": [], "captcha": False, "waf": False})


class KadParticipantSearchCoreTests(unittest.TestCase):
    def test_happy_path_by_inn(self):
        ops = FakeOps(["true", CLICKED, _row()])
        result = kad_search(inn="7701234567", ops=ops)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.reason, "search-ok")
        self.assertEqual(len(result.hits), 1)
        hit = result.hits[0]
        self.assertEqual(hit.case_number, "А40-308035/2025")
        self.assertEqual(hit.court, "АС Москвы")
        self.assertEqual(hit.judge, "Шуваева М. В.")
        self.assertEqual(hit.parties, ["ООO Истец", "ООO Ответчик"])
        self.assertTrue(hit.url.startswith("https://kad.arbitr.ru/Card/"))
        self.assertEqual(result.query["inn"], "7701234567")

    def test_happy_path_by_name(self):
        ops = FakeOps(["true", CLICKED, _row()])
        result = kad_search(name='ООО "Истец"', ops=ops)
        self.assertTrue(result.ok)
        self.assertEqual(result.query["name"], 'ООО "Истец"')

    def test_normalizes_latin_case_number(self):
        ops = FakeOps(["true", CLICKED, _row("A40-308035/2025")])
        result = kad_search(inn="7701234567", ops=ops)
        self.assertEqual(result.hits[0].case_number, "А40-308035/2025")

    def test_empty_input_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            kad_search(ops=FakeOps([]))
        with self.assertRaises(ValidationError):
            kad_search(inn="   ", name="", ops=FakeOps([]))

    def test_captcha_returns_degraded(self):
        ops = FakeOps(["true", CLICKED, CAPTCHA])
        result = kad_search(inn="7701234567", ops=ops)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "degraded")
        self.assertTrue(result.reason.startswith("PRACTICE_CAPTCHA"))
        self.assertEqual(result.hits, [])

    def test_waf_returns_degraded(self):
        ops = FakeOps(["true", CLICKED, WAF])
        result = kad_search(inn="7701234567", ops=ops)
        self.assertEqual(result.status, "degraded")
        self.assertTrue(result.reason.startswith("WAF_BLOCKED"))

    def test_no_results_degraded(self):
        ops = FakeOps(["true", CLICKED] + [EMPTY] * 10)
        result = kad_search(inn="7701234567", ops=ops)
        self.assertEqual(result.status, "degraded")
        self.assertTrue(result.reason.startswith("PRACTICE_EMPTY"))

    def test_form_not_ready_degraded(self):
        ops = FakeOps(["false"] * 20)
        result = kad_search(inn="7701234567", ops=ops)
        self.assertEqual(result.status, "degraded")
        self.assertTrue(result.reason.startswith("FORM_NOT_READY"))

    def test_result_to_dict_structure(self):
        ops = FakeOps(["true", CLICKED, _row()])
        result = kad_search(inn="7701234567", ops=ops)
        d = result.to_dict()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["count"], 1)
        self.assertIn("hits", d)
        self.assertIn("provenance", d)
        self.assertIn("query", d["provenance"])


class KadParticipantSearchPublicApiTests(unittest.TestCase):
    def test_public_api_ok(self):
        ops = FakeOps(["true", CLICKED, _row()])
        out = public_kad_search(inn="7701234567", browser_ops=ops)
        self.assertIsInstance(out, ApiOutcome)
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.status, "ok")
        self.assertEqual(out.source, "kad-search")
        self.assertEqual(out.extensions["count"], 1)
        self.assertEqual(out.extensions["hits"][0]["case_number"],
                         "А40-308035/2025")

    def test_public_api_validation_error(self):
        out = public_kad_search()
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "error")
        self.assertIn("validation error", out.reason)

    def test_public_api_captcha_degraded(self):
        ops = FakeOps(["true", CLICKED, CAPTCHA])
        out = public_kad_search(inn="7701234567", browser_ops=ops)
        self.assertEqual(out.status, "degraded")
        self.assertTrue(out.reason.startswith("PRACTICE_CAPTCHA"))


if __name__ == "__main__":
    unittest.main()
