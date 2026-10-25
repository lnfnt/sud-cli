"""Case history: all acts of a case across instances (one call).

SudAct result titles embed related case numbers, e.g.::

    Решение № 02-0387/2025 02-0387/2025(02-3696/2024)~М-3025/2024

Parenthesized numbers are related-instance case numbers (verified live
2026-09-10: searching '02-0387/2025' in sudact regular section returns
10 documents whose titles reference sibling case numbers). This module
chains: search by case number → collect related numbers from titles →
search each → group all documents into a case history.

Requires a JS-capable fetcher (Camofox) — same as search_practice.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..errors import EmptyPayloadError
from .search import search, SearchResult, SUDACT_SEARCH_VERSION

CASE_HISTORY_VERSION = "sudact-case-history-0.1.0"

# Related case numbers in parens: (А40-308035/2025) or (02-3696/2024)
_RELATED_RE = re.compile(r"\(([А-ЯA-Z0-9]{1,6}-\d{1,7}/\d{2,4})\)")
_CASE_NUM_RE = re.compile(r"[А-ЯA-Z0-9]{1,6}-\d{1,7}/\d{2,4}")

_SECTIONS = ("arbitral", "regular", "magistrate", "vsrf")


def _extract_related_numbers(titles: list[str]) -> list[str]:
    """Related case numbers from result titles (deduped, ordered)."""
    seen: list[str] = []
    for title in titles:
        for num in _RELATED_RE.findall(title):
            if num not in seen:
                seen.append(num)
    return seen


def search_case_history(
    case_number: str,
    fetcher: Callable[[str], str],
    *,
    section: str = "arbitral",
    max_related: int = 5,
    limit: int = 10,
) -> dict[str, Any]:
    """Build a case history: all acts for a case number + related
    instance numbers found in sudact.

    Returns a dict (agent-facing):

        {
          "case_number": "...",
          "section": "regular",
          "instances": {
             "<case_number>": {"documents": [...], "next_page": N|None},
             ...
          },
          "related_numbers": [...],
          "total_documents": N,
          "sections_skipped": {"arbitral": "empty"},
          "parser_version": ...
        }

    The primary case number is searched first; related numbers are
    extracted from result titles and searched too (up to max_related).
    Sections are tried in order until hits appear (a case number lives
    in one section: arbitral OR regular OR ...).
    """
    if not case_number or not case_number.strip():
        raise ValueError("case_number must be non-empty")
    case_number = case_number.strip()

    def _one_search(number: str, sect: str) -> SearchResult:
        return search(number, fetcher, section=sect, page=1, limit=limit)

    # Find the section that actually has this case number. EmptyPayloadError
    # means "this section has no results" and is skipped; any other error
    # (WAF/challenge/network) fails the whole call loud and clear.
    primary: SearchResult | None = None
    used_section = ""
    sections_skipped: dict[str, str] = {}
    for sect in _SECTIONS:
        try:
            res = _one_search(case_number, sect)
        except EmptyPayloadError:
            sections_skipped[sect] = "empty"
            continue
        if res and res.total_on_page > 0:
            primary = res
            used_section = sect
            break

    if primary is None:
        return {
            "case_number": case_number,
            "instances": {},
            "related_numbers": [],
            "total_documents": 0,
            "error": "no documents found in any section",
            "sections_skipped": sections_skipped,
            "parser_version": CASE_HISTORY_VERSION,
        }

    instances: dict[str, Any] = {
        case_number: primary.to_dict(),
    }
    related: list[str] = []
    # Related numbers from the primary result titles
    all_titles = [r["title"] for r in primary.results]
    for num in _extract_related_numbers(all_titles):
        if num == case_number or num in related:
            continue
        related.append(num)
        if len(related) >= max_related:
            break

    # Search each related number in the same section. Empty results are
    # recorded honestly; challenges/network errors still fail loudly.
    for num in related:
        try:
            res = _one_search(num, used_section)
        except EmptyPayloadError:
            instances[num] = {"documents": [], "next_page": None,
                              "error": "no documents found"}
        else:
            if res and res.total_on_page > 0:
                instances[num] = res.to_dict()
            else:
                instances[num] = {"documents": [], "next_page": None,
                                  "error": "no documents found"}

    total = sum(i.get("count", 0) for i in instances.values()
                if isinstance(i, dict))
    return {
        "case_number": case_number,
        "section": used_section,
        "instances": instances,
        "related_numbers": related,
        "total_documents": total,
        "sections_skipped": sections_skipped,
        "parser_version": CASE_HISTORY_VERSION,
    }


__all__ = ["search_case_history", "CASE_HISTORY_VERSION"]
