"""ГАС «Правосудие» (sudrf) import parser — offline, saved HTML.

Input: a saved GAS "Документ для печати" / case-card page
(<court>.sudrf.ru/modules.php?name=sud_delo&...case_uid=...).
Encoding: windows-1251 (detected; utf-8 also supported).

Verified live 2026-09-10 against oblsud.mo.sudrf.ru (апелляция на
Балашихинский городской суд, fixture gas-print-2a-2939-2025.html).
Document structure (line-based after tag-strip):

    <title area> Суд (Московский областной суд) ...
    ...Административные дела (КАС РФ) - апелляция
    ДЕЛО № 33а-29651/2025
    ДЕЛО
    Уникальный идентификатор дела
    50RS0001-01-2025-001560-80        <- УИД (У-И-Д format)
    Дата поступления / Категория дела / Судья / Дата рассмотрения /
    Результат рассмотрения
    РАССМОТРЕНИЕ В НИЖЕСТОЯЩЕМ СУДЕ   <- first instance info
    ДВИЖЕНИЕ ДЕЛА                     <- event table
    УЧАСТНИКИ                         <- parties table
    СУДЕБНЫЕ АКТЫ                     <- act links (may need JS)

Fail-closed: wrong encoding/empty/missing case number -> error;
identity mismatch -> error, no snapshot. PII: parties are parsed as
role+name pairs (public register data), no INN/addresses beyond what
GAS itself publishes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import (EmptyPayloadError, MalformedPayloadError,
                      WafDetectedError)
from ..normalize_refs import normalize_case_ref, refs_match
from ..schema import CaseSnapshot, compute_raw_hash
from ..sources.base import RawSourceResult

SUDRF_PARSER_VERSION = "sudrf-import-0.1.0"

CHALLENGE_MARKERS = (
    "доступ ограничен", "подтвердите, что вы не робот",
    "ddos-guard", "captcha", "Warning: Security Risk",
)

_SECTION_HEADERS = ("ДЕЛО", "РАССМОТРЕНИЕ В НИЖЕСТОЯЩЕМ СУДЕ",
                    "ДВИЖЕНИЕ ДЕЛА", "УЧАСТНИКИ", "СУДЕБНЫЕ АКТЫ")

# УИД вида 50RS0001-01-2025-001560-80
_UID_RE = re.compile(r"[0-9]{2}RS\d{4}-\d{2}-\d{4}-\d{5,7}-\d{2,5}")


def _decode(raw: bytes | str) -> str:
    """Decode GAS bytes: try utf-8, fall back to cp1251 (GAS default)."""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _html_to_lines(html: str) -> list[str]:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    text = text.replace("&amp;", "&").replace("&laquo;", "«")
    text = text.replace("&raquo;", "»").replace("&mdash;", "—")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"\n\s*\n+", "\n", text)
    return [l.strip() for l in text.split("\n") if l.strip()]


def extract_full_text(html: str) -> str:
    """Extract the full act text (после шапки, до footer) from a saved
    GAS document page. Plain text, cp1251-aware."""
    if not html:
        return ""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    for ent, ch in (("&nbsp;", " "), ("&#160;", " "), ("&amp;", "&"),
                    ("&laquo;", "«"), ("&raquo;", "»"), ("&mdash;", "—"),
                    ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(ent, ch)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # Обрезаем шапку (до "ДЕЛО №") и футер (после nav-хвоста)
    start = 0
    for i, l in enumerate(lines):
        if l.startswith("ДЕЛО №") or "решение" in l.lower()[:20]:
            start = max(0, i - 2)
            break
    end = len(lines)
    for i in range(len(lines) - 1, len(lines) - 20, -1):
        if i < len(lines) and ("Все права защищены" in lines[i]
                               or "наверх" in lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def detect_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in CHALLENGE_MARKERS)


def _parse_kv_sections(lines: list[str]) -> dict[str, str]:
    """Parse ДЕЛО section: alternating label/value lines."""
    out: dict[str, str] = {}
    labels = ("Уникальный идентификатор дела", "Дата поступления",
              "Категория дела", "Судья", "Дата рассмотрения",
              "Результат рассмотрения")
    for i, line in enumerate(lines):
        if line in labels and i + 1 < len(lines):
            out[line] = lines[i + 1]
    return out


def _parse_lower_court(lines: list[str]) -> dict[str, str]:
    """Parse РАССМОТРЕНИЕ В НИЖЕСТОЯЩЕМ СУДЕ section."""
    out: dict[str, str] = {}
    try:
        idx = lines.index("РАССМОТРЕНИЕ В НИЖЕСТОЯЩЕМ СУДЕ")
    except ValueError:
        return out
    labels = ("Суд (судебный участок) первой инстанции",
              "Номер дела в первой инстанции",
              "Судья (мировой судья) первой инстанции")
    for i in range(idx, min(idx + 10, len(lines))):
        line = lines[i]
        if line in labels and i + 1 < len(lines):
            out[line] = lines[i + 1]
    return out


def _parse_motion(lines: list[str]) -> list[dict[str, str]]:
    """Parse ДВИЖЕНИЕ ДЕЛА table: event blocks of 6-7 lines."""
    try:
        start = lines.index("ДВИЖЕНИЕ ДЕЛА")
    except ValueError:
        return []
    events: list[dict[str, str]] = []
    i = start + 1
    # skip header rows
    while i < len(lines) and lines[i] not in ("Наименование события",):
        i += 1
    i += 1  # skip "Наименование события"
    n = len(lines)
    while i < n:
        # event starts with a non-date line followed by a date
        if i + 1 < n and re.match(r"\d{2}\.\d{2}\.\d{4}$", lines[i + 1] or ""):
            event = {"event": lines[i], "date": lines[i + 1]}
            j = i + 2
            # optional: time, place, result, grounds, note, placement
            tail = []
            while j < n and len(tail) < 5 and not (
                    j + 1 < n and re.match(
                        r"\d{2}\.\d{2}\.\d{4}$", lines[j + 1] or "")):
                if lines[j] in ("Наименование события", "ДЕЛО",
                                "РАССМОТРЕНИЕ В НИЖЕСТОЯЩЕМ СУДЕ",
                                "УЧАСТНИКИ", "СУДЕБНЫЕ АКТЫ"):
                    break
                tail.append(lines[j])
                j += 1
            if len(tail) >= 3:
                event["time"] = tail[0] if re.match(r"^\d{1,2}:\d{2}$",
                                                    tail[0]) else ""
                event["result"] = tail[2] if len(tail) > 2 else ""
                events.append(event)
            i = j
        else:
            i += 1
    return events


def _parse_participants(lines: list[str]) -> list[dict[str, str]]:
    """Parse УЧАСТНИКИ table: role -> name (+ optional INN/OGRN)."""
    try:
        start = lines.index("УЧАСТНИКИ", 1)
    except ValueError:
        return []
    parts: list[dict[str, str]] = []
    i = start + 1
    roles = ("ЗАИНТЕРЕСОВАННОЕ ЛИЦО", "АДМИНИСТРАТИВНЫЙ ОТВЕТЧИК",
             "АДМИНИСТРАТИВНЫЙ ИСТЕЦ", "ИСТЕЦ", "ОТВЕТЧИК",
             "ТРЕТЬЕ ЛИЦО", "ПРЕДСТАВИТЕЛЬ")
    while i < len(lines):
        line = lines[i]
        if line in _SECTION_HEADERS:
            break
        if line in roles:
            name = lines[i + 1] if i + 1 < len(lines) else ""
            if name and name not in roles and name not in _SECTION_HEADERS:
                parts.append({"role": line, "name": name})
        i += 1
    return parts


@dataclass(frozen=True)
class SudrfRecord:
    case_number: str
    uid: str
    court: str
    judge: str
    result: str
    category: str
    first_instance_court: str
    first_instance_number: str
    first_instance_judge: str
    events: tuple[dict[str, str], ...]
    participants: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]
    source: str
    source_url: str
    retrieved_at: str
    raw_artifact_hash: str

    def to_case_snapshot(self) -> CaseSnapshot:
        # Motion events -> canonical timeline
        timeline = []
        for ev in self.events:
            name = ev.get("result") or ev.get("event") or "Событие"
            d = ev.get("date", "")
            if re.match(r"\d{2}\.\d{2}\.\d{4}$", d):
                dd, mm, yy = d.split(".")
                iso = f"{yy}-{mm}-{dd}"
            else:
                iso = d
            timeline.append({"date": iso, "name": name,
                             "published": True, "pdf_url": ""})
        payload = {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge or "unknown",
            "status": self.result or self.title_short(),
            "participants": {"plaintiff": [], "respondent": [],
                             "third_parties": []},
            "next_hearing": None,
            "last_event": None,
            "events_timeline": timeline,
            "updated_at": self.retrieved_at[:10],
        }
        return normalize_snapshot(payload, source=self.source,
                                  source_url=self.source_url,
                                  retrieved_at=self.retrieved_at,
                                  raw_artifact_hash=self.raw_artifact_hash,
                                  warnings=self.warnings,
                                  parser_version=SUDRF_PARSER_VERSION)

    def title_short(self) -> str:
        return f"ГАС: {self.court}"


def normalize_snapshot(payload: dict, **kwargs: Any) -> CaseSnapshot:
    from ..normalize import normalize
    return normalize(payload, **kwargs)


def parse_sudrf_html(html: str, *, case_ref: str, source: str,
                     source_url: str, retrieved_at: str,
                     raw_artifact_hash: str) -> SudrfRecord:
    """Parse a saved GAS document page. Fail-closed."""
    if not html or not html.strip():
        raise EmptyPayloadError(source)
    low = html.lower()
    if any(m in low for m in CHALLENGE_MARKERS):
        from ..errors import WafDetectedError
        raise WafDetectedError(source, 200)

    lines = _html_to_lines(html)

    # Court name: first meaningful line (before nav junk)
    court = ""
    for line in lines[:30]:
        if "суд" in line.lower() and 5 < len(line) < 80 \
                and "версия" not in line.lower():
            court = line
            break
    if not court:
        raise MalformedPayloadError(source, "court name not found in page")

    # Case category line ("Административные дела (КАС РФ) - апелляция")
    category_line = ""
    for line in lines:
        if "апелляция" in line.lower() or "первая инстанция" in line.lower() \
                or "кассация" in line.lower():
            category_line = line
            break

    # Case number from ДЕЛО № line
    case_num_in_doc = ""
    for line in lines:
        m = re.match(r"ДЕЛО\s*№\s*(.+)", line, re.I)
        if m:
            case_num_in_doc = m.group(1).strip()
            break
    if not case_num_in_doc:
        raise MalformedPayloadError(
            source, "no «ДЕЛО №» line — not a GAS case document")

    # identity: requested case_ref must appear in the document
    from ..normalize_refs import normalize_case_ref, refs_match
    doc_numbers = set(_CASE_NUM_RE.findall(html))
    if not any(refs_match(case_ref, n) for n in doc_numbers):
        raise MalformedPayloadError(
            source, f"case identity mismatch: '{case_ref}' not found in "
                    f"document (found: {sorted(doc_numbers)[:5]})")
    case_num_canonical = normalize_case_ref(case_ref)

    kv = _parse_kv_sections(lines)
    uid = kv.get("Уникальный идентификатор дела", "")
    uid_m = _UID_RE.search(html)
    if not uid and _UID_RE.search(html):
        uid = _UID_RE.search(html).group(0)

    lower_court = _parse_lower_court(lines)
    motion = _parse_motion(lines)
    participants = _parse_participants(lines)

    warnings: list[str] = []
    if not uid:
        warnings.append("УИД not found")
    if not motion:
        warnings.append("ДВИЖЕНИЕ ДЕЛА table not found/empty")

    first_number = lower_court.get("Номер дела в первой инстанции", "")
    court_field = kv.get("Судья", "")  # kv["Судья"] is the judge name
    return SudrfRecord(
        case_number=normalize_case_ref(case_ref),
        uid=uid,
        court=court,
        judge=kv.get("Судья", ""),
        result=kv.get("Результат рассмотрения", ""),
        category=category_line,
        first_instance_court=lower_court.get(
            "Суд (судебный участок) первой инстанции", ""),
        first_instance_number=first_number,
        first_instance_judge=lower_court.get(
            "Судья (мировой судья) первой инстанции", ""),
        events=tuple(motion),
        participants=tuple(participants),
        warnings=tuple(warnings),
        source=source,
        source_url=source_url,
        retrieved_at=retrieved_at,
        raw_artifact_hash=raw_artifact_hash,
    )


_CASE_NUM_RE = re.compile(r"[0-9]{1,4}[А-Яа-яA-Za-z]{0,3}-\d{1,6}/\d{4}")


def _html_to_lines(html: str) -> list[str]:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    text = text.replace("&amp;", "&").replace("&laquo;", "«")
    text = text.replace("&raquo;", "»").replace("&mdash;", "—")
    text = re.sub(r"\n\s*\n+", "\n", text)
    return [l.strip() for l in text.split("\n") if l.strip()]


class SudrfImportProvider:
    """Local import adapter: one saved GAS HTML file (cp1251 or utf-8)."""

    name = "sudrf-import"
    media_type = "text/html"
    accepts_fixtures = False

    def __init__(self, html_path: Path | str, case_ref: str):
        from ..normalize_refs import normalize_case_ref, refs_match
        self._refs_match = refs_match
        self.html_path = Path(html_path)
        self.case_ref = case_ref

    def lookup(self, case_ref: str) -> RawSourceResult:
        from ..errors import SourceUnavailableError
        if not self._refs_match(case_ref, self.case_ref):
            raise SourceUnavailableError(
                self.name,
                f"requested '{case_ref}' but import was staged for "
                f"'{self.case_ref}'")
        if not self.html_path.is_file():
            raise SourceUnavailableError(
                self.name, f"import file missing: {self.html_path}")
        raw = self.html_path.read_bytes()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body = raw.decode("cp1251", errors="replace")
        return RawSourceResult(
            source=f"{self.name}:{self.html_path.name}",
            source_url=f"file://{self.html_path.resolve()}",
            status_code=200,
            body=body,
            media_type="text/html",
        )


def fetch_sudrf_html(provider, case_ref: str, retrieved_at: str) -> Any:
    """lookup -> classify -> parse -> FetchOutcome."""
    from ..errors import CaseSearchError
    from ..pipeline import DEGRADED, ERROR, OK, FetchOutcome
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    if raw.media_type != "text/html":
        return FetchOutcome(ERROR, None, "sudrf pipeline requires text/html")
    try:
        record = parse_sudrf_html(
            raw.body, case_ref=case_ref, source=raw.source,
            source_url=raw.source_url, retrieved_at=retrieved_at,
            raw_artifact_hash=compute_raw_hash(raw.body))
        snapshot = record.to_case_snapshot()
    except WafDetectedError as exc:
        return FetchOutcome(DEGRADED, None, str(exc), exc.retry_after)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    return FetchOutcome(OK, snapshot, "normalized")


__all__ = ["SudrfRecord", "SudrfImportProvider", "parse_sudrf_html",
           "fetch_sudrf_html", "extract_full_text",
           "SUDRF_PARSER_VERSION"]
