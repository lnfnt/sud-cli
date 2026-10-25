"""Tests for sudact case_history resilience and fail-closed behavior."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (  # noqa: E402
    EmptyPayloadError,
    WafDetectedError,
)
from case_search.sudact.case_history import (  # noqa: E402
    CASE_HISTORY_VERSION,
    search_case_history,
)
from case_search.sudact.search import FetchError, SearchResult  # noqa: E402


def _make_result(
    number: str,
    section: str,
    count: int = 3,
    titles: list[str] | None = None,
) -> SearchResult:
    if titles is None:
        titles = [f"Решение № {number} от 01.01.2020 г."] * count
    results = tuple(
        {
            "position": i + 1,
            "url": f"https://sudact.ru/{section}/doc/{i}/",
            "doc_path": f"/{section}/doc/{i}/",
            "title": titles[i],
            "court": "Московский городской суд",
            "dispute": "",
            "date": "01 января 2020",
            "snippet": "",
            "relevance": 1.0,
        }
        for i in range(count)
    )
    return SearchResult(
        query=number,
        section=section,
        page=1,
        total_on_page=count,
        next_page=None,
        results=results,
        source_url=f"https://sudact.ru/{section}/doc/?{section}-txt={number}",
    )


def _noop_fetcher(url: str) -> str:
    return ""


class SectionResilienceTests(unittest.TestCase):
    """EmptyPayloadError from one section must not kill the whole history."""

    def test_regular_hits_with_other_sections_empty_builds_history(self):
        """Reproduces the live 7-2536/2017 defect: regular has 3 docs."""

        def side_effect(number, fetcher, *, section, page, limit):
            if section == "regular":
                return _make_result(number, section, count=3)
            raise EmptyPayloadError("sudact-search: no result blocks")

        with patch("case_search.sudact.case_history.search") as mock_search:
            mock_search.side_effect = side_effect
            out = search_case_history("7-2536/2017", _noop_fetcher)

        self.assertEqual(out["section"], "regular")
        self.assertEqual(out["total_documents"], 3)
        self.assertIn("7-2536/2017", out["instances"])
        self.assertEqual(out["instances"]["7-2536/2017"]["count"], 3)
        self.assertEqual(out["sections_skipped"], {"arbitral": "empty"})
        self.assertEqual(out["parser_version"], CASE_HISTORY_VERSION)

    def test_all_sections_empty_returns_honest_error(self):
        with patch(
            "case_search.sudact.case_history.search",
            side_effect=EmptyPayloadError("sudact-search: no result blocks"),
        ):
            out = search_case_history("7-0000/0000", _noop_fetcher)

        self.assertEqual(out["error"], "no documents found in any section")
        self.assertEqual(out["total_documents"], 0)
        self.assertEqual(out["instances"], {})
        self.assertEqual(
            out["sections_skipped"],
            {
                "arbitral": "empty",
                "regular": "empty",
                "magistrate": "empty",
                "vsrf": "empty",
            },
        )


class FailClosedTests(unittest.TestCase):
    """Challenge / WAF / network errors must not be silently swallowed."""

    def test_waf_in_section_fails_loudly(self):
        def side_effect(number, fetcher, *, section, page, limit):
            if section == "arbitral":
                raise EmptyPayloadError("sudact-search: no result blocks")
            if section == "regular":
                raise WafDetectedError("sudact-search", 200)
            raise EmptyPayloadError("sudact-search: no result blocks")

        with patch(
            "case_search.sudact.case_history.search",
            side_effect=side_effect,
        ):
            with self.assertRaises(WafDetectedError):
                search_case_history("7-2536/2017", _noop_fetcher)

    def test_network_error_in_section_fails_loudly(self):
        with patch(
            "case_search.sudact.case_history.search",
            side_effect=FetchError("network down"),
        ):
            with self.assertRaises(FetchError):
                search_case_history("7-2536/2017", _noop_fetcher)


class LiveCaptureVerificationTests(unittest.TestCase):
    """The live captures used for verification must remain intact."""

    def test_live_fixtures_exist_and_contain_case_numbers(self):
        f1 = REPO_ROOT / "fixtures" / "sudact" / "live-7-2536-2017.json"
        f2 = REPO_ROOT / "fixtures" / "sudact" / "live-7-1013-2026.json"
        self.assertTrue(f1.exists(), "live-7-2536-2017.json fixture missing")
        self.assertTrue(f2.exists(), "live-7-1013-2026.json fixture missing")
        self.assertIn("7-2536/2017", f1.read_text(encoding="utf-8"))
        self.assertIn("7-1013/2026", f2.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
