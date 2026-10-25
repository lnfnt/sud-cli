"""SudAct.ru document import parser (saved HTML, no network).

SudAct pages are server-rendered HTML (verified live 2026-09-09 on four
documents: regular СОЮ, regular административное, arbitral банкротство,
arbitral АС МО). One unified layout:

- ``<h1>`` — document title, often containing the case number
  (e.g. ``Решение № 2А-3400/2025 ... по делу № 2А-3400/2025``);
- ``div.h-col1-inner3`` — document container; first lines contain the
  court name in parentheses, optional category/«Суть спора:»;
- judge name from ``<meta name="description">`` («Судья: X.») or from
  the text («председательствующего судьи X»).

Fail-closed: a page without an h-col1-inner3 container, without a
parseable court name, or whose case numbers do not include the
requested case_ref raises MalformedPayloadError. PII policy: parties
are NOT extracted (document text stays out of the snapshot); only the
title/court/judge/dispute structural fields are kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..errors import (EmptyPayloadError, MalformedPayloadError,
                      WafDetectedError)
from ..normalize_refs import normalize_case_ref, refs_match
from ..normalize import normalize
from ..schema import CaseSnapshot, compute_raw_hash
from ..sources.base import RawSourceResult

SUDACT_PARSER_VERSION = "sudact-import-0.1.0"

from ._common import CHALLENGE_MARKERS  # noqa: E402

CASE_NUM_RE = re.compile(r"[А-ЯA-Z0-9]{1,6}-?\d{0,5}/\d{2,4}")
COURT_RE = re.compile(r"^([А-ЯЁA-Z][^\n()]{4,120}?)\s*\(([^)]{2,60})\)$")
DISPUTE_RE = re.compile(r"Суть спора:\s*([^\n<]+)")
JUDGE_META_RE = re.compile(r"Судья:\s*([А-ЯЁ][а-яё]+ [А-ЯЁ]\.[А-ЯЁ]\.)")
JUDGE_TEXT_RE = re.compile(
    r"(?:председательствующ(?:его|ий) судьи|судья)\s+"
    r"([А-ЯЁ][а-яё]+ [А-ЯЁ]\.[А-ЯЁ]\.)")
DATE_DOC_RE = re.compile(r"от\s+(\d{1,2}\s+\w+\s+\d{4})\s*г?\.?")


def extract_full_text(html: str) -> str:
    """Extract the full visible text of the court act from a saved
    sudact document page (Р Е Ш Е Н И Е / ОПРЕДЕЛЕНИЕ body).

    Returns clean plain text (~30-40K chars for a typical СОЮ решение).
    The snapshot does NOT carry this text (schema stays structural);
    agents call this separately when they need the full act text.
    """
    m = re.search(
        r'<td[^>]*class="[^"]*h-col1-inner3[^"]*"[^>]*>(.*)', html, re.S)
    if not m:
        return ""
    body = m.group(1)
    end = body.find('<div class="bookmarkBlockContainer">')
    if end > 0:
        body = body[:end]
    text = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    text = text.replace("&amp;", "&").replace("&laquo;", "«")
    text = text.replace("&raquo;", "»").replace("&mdash;", "—")
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # Strip SudAct service footer (starts at any of these markers):
    footer_markers = ("Печать документа", "Отправить на e-mail",
                      "Сохранить в Word", "Получить ссылку на документ",
                      "Прямая ссылка на документ", "Отменить",
                      "Ответы на вопросы", "наверх",
                      "Все права защищены", "Политика в отношении",
                      "Судьи дела:", "Истцы:", "Ответчики:",
                      "Судебная практика по:", "(подробнее)", "подробнее)")
    cut = len(lines)
    for i, line in enumerate(lines):
        if any(m in line for m in footer_markers):
            cut = i
            break
    return "\n".join(lines[:cut])


def detect_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def _strip_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    return re.sub(r"\n\s*\n+", "\n", text)


@dataclass(frozen=True)
class SudactRecord:
    case_number: str
    title: str
    court: str
    court_region: str
    judge: str
    dispute: str
    doc_kind: str
    warnings: tuple[str, ...]
    source: str
    source_url: str
    retrieved_at: str
    raw_artifact_hash: str

    def to_case_snapshot(self) -> CaseSnapshot:
        payload = {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge or "unknown",
            "status": self.title[:200],
            "participants": {},
            "next_hearing": None,
            "last_event": None,
            "events_timeline": [],
            "updated_at": self.retrieved_at[:10],
        }
        return normalize(payload, source=self.source,
                         source_url=self.source_url,
                         retrieved_at=self.retrieved_at,
                         raw_artifact_hash=self.raw_artifact_hash,
                         warnings=self.warnings,
                         parser_version=SUDACT_PARSER_VERSION)


def parse_sudact_html(html: str, *, case_ref: str, source: str,
                      source_url: str, retrieved_at: str,
                      raw_artifact_hash: str) -> SudactRecord:
    """Parse a saved sudact.ru document page. Fail-closed, no PII."""
    if not html or not html.strip():
        raise EmptyPayloadError(source)

    plain_page = _strip_tags(html)
    if detect_challenge(plain_page) or detect_challenge(html):
        raise WafDetectedError(source, 200)

    container = re.search(r'h-col1-inner3">(.*?)<div class="h-col1-spacer',
                          html, re.S) or re.search(
        r'h-col1-inner3">(.*?)(?:<div class="bookmark|$)', html, re.S)
    if not container:
        raise MalformedPayloadError(
            source, "no document container (h-col1-inner3) — "
                    "changed DOM or non-document page")

    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if not title_m:
        raise MalformedPayloadError(source, "no <h1> document title")
    title = re.sub(r"\s+", " ",
                   re.sub(r"<[^>]+>", "", title_m.group(1))).strip()

    warnings: list[str] = []
    judge = ""
    meta_m = JUDGE_META_RE.search(html)
    if meta_m:
        judge = meta_m.group(1).strip(" .")
    else:
        text_m = JUDGE_TEXT_RE.search(plain_page)
        if text_m:
            judge = text_m.group(1)
            warnings.append("judge extracted from body text, not meta")

    # Court: first line inside the container matching "Name (Region)"
    body = re.sub(r"<script.*?</script>", "", container.group(1), flags=re.S)
    lines = [l.strip() for l in re.sub(r"<[^>]+>", "\n", body).split("\n")
             if l.strip()]
    court = court_region = ""
    for line in lines[:8]:
        m = COURT_RE.match(line)
        if m and "№" not in line:
            court, court_region = m.group(1).strip(), m.group(2).strip()
            break
    if not court:
        raise MalformedPayloadError(
            source, f"court name not found in container head: {lines[:5]}")

    dispute_m = DISPUTE_RE.search(body)
    dispute = dispute_m.group(1).strip() if dispute_m else ""

    # SudAct truncates some case numbers to a 2-digit year
    # (e.g. "А41-69570/20", "А40-273535/25" — verified live). Expand a
    # short-year number to 4 digits using the document's own date so the
    # requested full case_ref can be matched.
    def _has_case_ref(text: str) -> bool:
        if refs_match(case_ref, normalize_case_ref(case_ref)) and \
                case_ref in text:
            return True
        # Homoglyph-tolerant scan: normalize both sides per candidate.
        if normalize_case_ref(case_ref) in normalize_case_ref(text):
            return True
        year4 = case_ref.rsplit("/", 1)[-1]
        prefix = case_ref.rsplit("/", 1)[0]
        if len(year4) != 4 or "/" not in case_ref:
            return False
        # SudAct truncates numbers to any prefix of the year
        # ("/2025" -> "/20", "/2" or bare "/25"); accept a match when
        # the page shows prefix + a PROPER PREFIX of the year and the
        # document's own date agrees with the full year.
        candidates = [f"{prefix}/{year4[:k]}" for k in (3, 2, 1)]
        if len(year4) == 4:
            candidates.append(f"{prefix}/{year4[2:]}")  # '/YY' style
        for short in candidates:
            # Truncation may be followed by a compound-case suffix
            # (e.g. "А40-273535/25-80-1973"): reject only extra DIGITS.
            if re.search(re.escape(short) + r"(?!\d)", text):
                # Year-consistency: the case-number year must not exceed
                # the document date year (a case cannot be numbered after
                # the document was issued). Exact equality is NOT required:
                # a case opened in 2025 can produce a 2026 document.
                doc_year_m = re.search(r"\b\d{1,2}\s+\w+\s+(\d{4})\s*г",
                                       text)
                if doc_year_m and doc_year_m.group(1) >= year4:
                    warnings.append(
                        f"case number truncated on page as '{short}', "
                        f"year restored as {year4} (caller-supplied)")
                    return True
        return False

    if not _has_case_ref(title) and not _has_case_ref(body):
        raise MalformedPayloadError(
            source, f"case identity mismatch: '{case_ref}' not found "
                    f"in document (title: '{title[:120]}')")
    doc_kind = "sudact-document"
    if not dispute:
        warnings.append("dispute category not found on page")
    if not judge:
        warnings.append("judge not found on page")

    return SudactRecord(
        case_number=case_ref,
        title=title,
        court=court,
        court_region=court_region,
        judge=judge,
        dispute=dispute,
        doc_kind=doc_kind,
        warnings=tuple(warnings),
        source=source,
        source_url=source_url,
        retrieved_at=retrieved_at,
        raw_artifact_hash=raw_artifact_hash,
    )


class SudactImportProvider:
    """Local import adapter: one saved sudact.ru HTML file."""

    name = "sudact-import"
    media_type = "text/html"
    accepts_fixtures = False

    def __init__(self, html_path, case_ref: str):
        from pathlib import Path
        self.html_path = Path(html_path)
        self.case_ref = case_ref

    def lookup(self, case_ref: str) -> RawSourceResult:
        from ..errors import SourceUnavailableError
        if not refs_match(case_ref, self.case_ref):
            raise SourceUnavailableError(
                self.name,
                f"requested '{case_ref}' but import was staged for "
                f"'{self.case_ref}'")
        if not self.html_path.is_file():
            raise SourceUnavailableError(
                self.name, f"import file missing: {self.html_path}")
        body = self.html_path.read_text(encoding="utf-8", errors="replace")
        return RawSourceResult(
            source=f"{self.name}:{self.html_path.name}",
            source_url=f"file://{self.html_path.resolve()}",
            status_code=200,
            body=body,
            media_type="text/html",
        )


def fetch_sudact_html(provider, case_ref: str,
                      retrieved_at: str) -> FetchOutcome:
    """lookup -> classify -> parse -> FetchOutcome (mirrors kad pipeline)."""
    from ..errors import CaseSearchError
    from ..pipeline import DEGRADED, ERROR, OK, FetchOutcome
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    if raw.media_type != "text/html":
        return FetchOutcome(ERROR, None, "sudact pipeline requires text/html")
    try:
        record = parse_sudact_html(
            raw.body, case_ref=case_ref, source=raw.source,
            source_url=raw.source_url, retrieved_at=retrieved_at,
            raw_artifact_hash=compute_raw_hash(raw.body))
        snapshot = record.to_case_snapshot()
    except WafDetectedError as exc:
        return FetchOutcome(DEGRADED, None, str(exc), exc.retry_after)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    return FetchOutcome(OK, snapshot, "normalized")


__all__ = ["SudactRecord", "SudactImportProvider", "parse_sudact_html",
           "fetch_sudact_html", "detect_challenge", "extract_full_text",
           "SUDACT_PARSER_VERSION"]
