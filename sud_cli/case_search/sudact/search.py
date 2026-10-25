"""SudAct practice search: query -> result list (no network in parser).

SudAct search is a server-rendered GET: ``/{section}/?<section>-txt=...``
returns a numbered result list (verified live 2026-09-10, 30 items per
page, pagination via ``?page=N``). One result block:

``<span class="numb">N.</span><h4><b><a href="/{section}/doc/ID/">TITLE</a>
</b></h4><div class="b-justice">COURT (SHORT)</div> ... Суть спора: ...``

The provider fetches via an injected fetcher (no network library here),
the parser is pure. Fail-closed: challenge page / no result blocks ->
degraded/error, never a fabricated list. Agent-facing contract:
``search()`` returns a JSON-serializable dict with results[] and
page info; each result's URL can be fed to the document import path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, NoReturn, Protocol

from ..errors import (CaseSearchError, EmptyPayloadError,
                      WafDetectedError)

SUDACT_SEARCH_VERSION = "sudact-search-0.1.0"

SECTIONS = ("arbitral", "regular", "magistrate", "vsrf")
DEFAULT_LIMIT = 10
MAX_LIMIT = 30

from ._common import CHALLENGE_MARKERS  # noqa: E402

_RESULT_RE = re.compile(
    r'<span class="numb">(\d+)\.</span>'
    r'<h4><b><a href="(/(?:arbitral|regular|magistrate|vsrf)/doc/[^"]+)"'
    r'[^>]*>(.*?)</a></b></h4>'
    r'(.*?)(?=<span class="numb">|$)', re.S)
_PAGES_RE = re.compile(r'[?&]page=(\d+)')
_DISPUTE_RE = re.compile(r"Суть спора:\s*([^<]+)")
_DATE_RE = re.compile(r"от\s+(\d{1,2}\s+\w+\s+\d{4})\s*г")


class FetchError(CaseSearchError):
    """Injected fetcher failed (network/timeout/HTTP >= 400)."""


class Fetcher(Protocol):
    def __call__(self, url: str) -> str: ...


@dataclass(frozen=True)
class SearchResult:
    query: str
    section: str
    page: int
    total_on_page: int
    next_page: int | None
    results: tuple[dict, ...]
    source_url: str
    limit: int = DEFAULT_LIMIT
    parser_version: str = SUDACT_SEARCH_VERSION

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "section": self.section,
            "page": self.page,
            "limit": self.limit,
            "total_on_page": self.total_on_page,
            "next_page": self.next_page,
            "count": len(self.results),
            "results": [dict(r) for r in self.results],
            "source_url": self.source_url,
            "parser_version": self.parser_version,
        }


def _js_required() -> NoReturn:
    """Raise an error signalling that JS-capable browser is required."""
    raise WafDetectedError(
        "sudact-search: search endpoint requires a JS-capable browser "
        "(sudact serves a 'Поиск не доступен' stub to plain HTTP "
        "clients); use the Camofox search path")


from ._common import strip_tags as _strip_tags  # noqa: E402


def parse_search_html(html: str, *, query: str, section: str,
                      page: int, source_url: str,
                      limit: int = DEFAULT_LIMIT) -> SearchResult:
    """Parse a search result page. Fail-closed."""
    if not html or not html.strip():
        raise EmptyPayloadError("sudact-search")
    visible = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                     flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", visible)
    low = visible.lower()
    if any(m in low for m in CHALLENGE_MARKERS):
        raise WafDetectedError("sudact-search", 200)
    # The search endpoint serves real results ONLY to JS-capable
    # browsers; plain HTTP clients get a "Поиск не доступен" stub.
    if "поиск не доступен" in low or ("отключен" in low and
                                      "javascript" in low):
        _js_required()

    results: list[dict] = []
    for num, url, title, rest in _RESULT_RE.findall(html):
        court_m = re.search(r'class="b-justice">([^<]+)', rest)
        dispute_m = _DISPUTE_RE.search(rest)
        date_m = _DATE_RE.search(title)
        # Snippet: sudact highlights matched words in
        # <span class="snippet_equal"> inside the result's r-col.
        snip_words = [s.strip() for s in re.findall(
            r'class="snippet_equal"[^>]*>(.*?)<', rest, re.S) if s.strip()]
        # Full visible text of the result block (fallback snippet)
        block_text = _strip_tags(rest)
        snippet = " ".join(snip_words) if snip_words else \
            block_text[:200]
        results.append({
            "position": int(num),
            "url": "https://sudact.ru" + url,
            "doc_path": url,
            "title": _strip_tags(title),
            "court": court_m.group(1).strip() if court_m else "",
            "dispute": dispute_m.group(1).strip() if dispute_m else "",
            "date": date_m.group(1) if date_m else "",
            "snippet": snippet[:300],
        })
    if not results:
        # Honest empty result set (no hits or changed DOM). Not a
        # silent success: agents branch on explicit error status.
        raise EmptyPayloadError("sudact-search: no result blocks "
                                "(no hits or changed DOM)")

    # SudAct silently ignores a non-matching query and returns its
    # default recent-acts list (verified live 2026-09-10). An agent
    # would read that as fake search hits. Guard: at least one query
    # word must appear in the page's visible result titles/text.
    import unicodedata
    _STOP_WORDS = frozenset({
        "и", "в", "на", "по", "с", "о", "об", "к", "за", "от", "до",
        "из", "не", "ни", "но", "а", "да", "же", "ли", "бы", "то",
        "для", "что", "при", "так", "его", "её", "их", "она", "они",
        "оно", "тот", "та", "то", "этот", "эта", "это", "все",
        "как", "ещё", "уже", "был", "была", "было", "были",
    })
    normalized_query = unicodedata.normalize("NFKC", query).lower()
    all_words = normalized_query.split()
    meaningful = [w for w in all_words if w not in _STOP_WORDS]
    guard_words = [w for w in meaningful if len(w) >= 3]
    if not guard_words and meaningful:
        guard_words = [w for w in meaningful if len(w) >= 2]
    if guard_words:
        titles_blob = _strip_tags(html).lower()
        coverage = sum(1 for w in guard_words if w in titles_blob)
        if coverage == 0:
            raise EmptyPayloadError(
                "sudact-search: query words not present in results — "
                "sudact returned its default list for a non-matching "
                "query")

    # Per-result relevance scoring (agent-facing): score = доля слов
    # запроса, найденных в title+court+dispute результата (0.0..1.0).
    # Агент может фильтровать по score вместо чтения сырых сниппетов.

    def _relevance(result: dict) -> float:
        blob = " ".join(str(result.get(k, "")) for k in
                        ("title", "court", "dispute",
                         "snippet")).lower()
        if not guard_words:
            return 1.0
        hits = sum(1 for w in guard_words if w in blob)
        return round(hits / len(guard_words), 2)

    for r in results:
        r["relevance"] = _relevance(r)
    # Порядок сохраняется как в выдаче sudact (position); relevance
    # добавляется как метадата для агентской фильтрации/сортировки.

    next_pages = [int(n) for n in _PAGES_RE.findall(html)]
    next_page = min(next_pages) if next_pages else None
    limited = results[:max(1, min(limit, MAX_LIMIT))]
    return SearchResult(
        query=query, section=section, page=page,
        total_on_page=len(limited), next_page=next_page,
        results=tuple(limited), source_url=source_url, limit=limit)


SECTION_ALIASES = {
    "regular": "regular", "soi": "regular", "гражданские": "regular",
    "civil": "regular", "coyu": "regular", "гпк": "regular", "gpk": "regular",
    "arbitral": "arbitral", "arbitration": "arbitral", "арбитраж": "arbitral",
    "apk": "arbitral", "апк": "arbitral", "kad": "arbitral",
    "magistrate": "magistrate", "мировые": "magistrate", "мировой": "magistrate",
    "vsrf": "vsrf", "вс": "vsrf", "верховный": "vsrf", "supreme": "vsrf",
}


def normalize_section(section: str) -> str:
    """Map human/Russian aliases to canonical sudact section."""
    s = (section or "").strip().lower()
    if s in SECTION_ALIASES:
        return SECTION_ALIASES[s]
    raise ValueError(
        f"unknown section {section!r}; valid: regular|soi|гражданские|gpk, "
        f"arbitral|арбитраж|apk, magistrate, vsrf")


def build_search_url(query: str, section: str = "arbitral",
                     page: int = 1) -> str:
    """Build the sudact search URL for a section.

    VERIFIED LIVE 2026-09-10: the working search endpoint is
    ``/{section}/doc/?{section}-txt=...`` — it requires a JS-capable
    browser (sudact returns a "Поиск не доступен: включите JavaScript"
    page to plain HTTP clients). The old ``/{section}/?...`` path
    SILENTLY ignores parameters and returns a default list — agents
    must never use it.
    """
    from urllib.parse import quote
    section = normalize_section(section)
    if section not in SECTIONS:
        raise ValueError(
            f"unknown section '{section}'; known: {SECTIONS}")
    base = f"https://sudact.ru/{section}/doc/"
    params = [f"{section}-txt={quote(query)}"]
    if page > 1:
        params.append(f"page={page}")
    return base + "?" + "&".join(params)


def search(query: str, fetcher: Fetcher, *, section: str = "arbitral",
           page: int = 1, limit: int = DEFAULT_LIMIT) -> SearchResult:
    """Search via injected fetcher. The fetcher does one GET and returns
    response body text; agent callers inject their HTTP layer."""
    if not query or not query.strip():
        raise ValueError("query must be non-empty")
    url = build_search_url(query, section, page)
    try:
        html = fetcher(url)
    except CaseSearchError:
        raise
    except Exception as exc:
        raise FetchError(f"sudact-search fetch failed: {exc}") from exc
    return parse_search_html(html, query=query, section=section,
                             page=page, source_url=url, limit=limit)


__all__ = ["normalize_section", "SECTION_ALIASES", "SearchResult", "search", "parse_search_html",
           "build_search_url", "SECTIONS", "SUDACT_SEARCH_VERSION",
           "FetchError"]
