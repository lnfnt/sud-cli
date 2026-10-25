"""KAD Arbitr case card parser.

Parses structured data extracted from a KAD case card page via Camofox
browser evaluate. KAD renders case data as a JS SPA — the caller must
first load the page in a browser, wait for content, and extract text.

Input format: dict with keys matching KAD's DOM structure (see
EXTRACT_EXPRESSION for the exact JS used to produce the input).

Fail-closed: missing case_number, empty parties, or ambiguous data raise
MalformedPayloadError. Never returns a partially-filled snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..errors import (CaseSearchError, EmptyPayloadError,
                      MalformedPayloadError, WafDetectedError)
from ..normalize import normalize
from ..schema import CaseSnapshot, compute_raw_hash
from ..normalize_refs import refs_match
from ..sources.base import Provider, RawSourceResult
from .instances import CASE_NUM_RE, detect_instance_level

KAD_PARSER_VERSION = "kad-camofox-0.2.1"

# JS expression to extract structured data from a KAD card page.
# Run this inside a Camofox/browser page via evaluate().
# Selectors verified against the live card А40-308035/2025 (2026-09-09):
#   parties live in the FIRST table.b-case-info
#   (td.plaintiffs / td.defendants / td.third / td.others, header row first);
#   court + judge live in the SECOND table.b-case-info
#   (td.arbitration a = court, td.first li = judge);
#   hearing line is .b-instanceAdditional.
# FIRST-INSTANCE ASSUMPTIONS in this expression (to revisit with live
# appeal/cassation cards):
#   - table.b-case-info index [1] is used for court/judge;
#   - document.querySelector(".b-instanceAdditional") returns the active
#     instance hearing line;
#   - events are collected globally across all visible .b-chrono-item nodes.
# The Python side now detects the active instance level from the case
# number and page context (see instances.py).
EXTRACT_EXPRESSION = r"""
JSON.stringify({
  case_number: document.querySelector(".js-case-header-case_num")?.innerText?.trim() || "",
  status: document.querySelector(".b-case-header-desc")?.innerText?.trim() || "",
  category: (document.querySelector("h2")?.innerText || "").replace(/\s+/g," ").trim(),
  court: document.querySelectorAll("table.b-case-info")[1]?.querySelector("td.arbitration a")?.innerText?.trim() || "",
  judge: document.querySelectorAll("table.b-case-info")[1]?.querySelector("td.first li")?.innerText?.trim() || "",
  plaintiffs: Array.from(document.querySelectorAll("table.b-case-info td.plaintiffs.first li, table.b-case-info td.plaintiffs li")||[]).map(e=>e.innerText.trim()).filter(t=>t && t.length>2).slice(0,20),
  respondents: Array.from(document.querySelectorAll("table.b-case-info td.defendants li")||[]).map(e=>e.innerText.trim()).filter(t=>t && t.length>2).slice(0,20),
  third_parties: Array.from(document.querySelectorAll("table.b-case-info td.third li")||[]).map(e=>e.innerText.trim()).filter(t=>t && t.length>2).slice(0,20),
  other: Array.from(document.querySelectorAll("table.b-case-info td.others li")||[]).map(e=>e.innerText.trim()).filter(t=>t && t.length>2).slice(0,20),
  next_hearing: document.querySelector(".b-instanceAdditional")?.innerText?.trim() || "",
  duration: document.querySelector(".case-dur")?.innerText?.trim() || "",
  decision_date: document.querySelector(".case-date")?.innerText?.trim() || "",
  instance_id: document.querySelector(".js-instanceId")?.value || "",
  events: (() => {
    const items = Array.from(document.querySelectorAll(".b-chrono-item")).slice(0,50);
    return items.map(e => ({
      date: e.querySelector(".case-date")?.innerText?.trim() || e.dataset.date || "",
      type: e.querySelector(".case-type")?.innerText?.trim() || "",
      subject: e.querySelector(".case-subject")?.innerText?.trim() || "",
      result: e.querySelector(".js-case-result-text")?.innerText?.trim() || "",
      pdf: e.querySelector('.js-case-result-text[href*="PdfDocument"]')?.href || "",
      published: !!e.querySelector(".b-case-publish_info"),
    }));
  })(),
  instances: Array.from(document.querySelectorAll(".b-chrono-item-header")).map(h => ({
    level: h.querySelector(".l-col strong")?.innerText?.trim() || "",
    court: h.querySelector(".instantion-name a")?.innerText?.trim() || "",
    instance_id: h.querySelector(".js-instanceId")?.value || "",
  })),
  full_text: document.body.innerText.substring(0, 10000)
})
"""

# WAF/challenge markers in KAD page text
CHALLENGE_MARKERS = (
    "pravocaptcha",
    "Доступ заблокирован",
    "Подтверждение действия",
    "captcha",
)

# Regex patterns (CASE_NUM_RE lives in instances.py to support appeal /
# cassation / supervision numbers as well as first-instance numbers).
DATE_RU_RE = re.compile(r"(\d{1,2})\.(\d{2})\.(\d{4})")
HEARING_RE = re.compile(
    r"(\d{1,2}\.\d{2}\.\d{4}),?\s+(\d{1,2}:\d{2}),?\s*(?:,?\s*Зал[^,]*.*)?")


def _parse_ru_date(text: str) -> str:
    """Convert 'DD.MM.YYYY' or 'DD месяц YYYY' to ISO, or empty string."""
    m = DATE_RU_RE.search(text)
    if m:
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return d.isoformat()
        except ValueError:
            pass
    # Try "11 ноября 2025" style
    months = {"января": "01", "февраля": "02", "марта": "03",
              "апреля": "04", "мая": "05", "июня": "06",
              "июля": "07", "августа": "08", "сентября": "09",
              "октября": "10", "ноября": "11", "декабря": "12"}
    m2 = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text.strip())
    if m2 and m2.group(2).lower() in months:
        try:
            d = date(int(m2.group(3)), int(months[m2.group(2).lower()]),
                     int(m2.group(1)))
            return d.isoformat()
        except ValueError:
            pass
    return ""


def _parse_hearing(text: str) -> dict[str, str] | None:
    """Parse '21.10.2026, 15:00 , Зал судебных заседаний № 6023'."""
    if not text:
        return None
    m = re.search(r"(\d{1,2}\.\d{2}\.\d{4})\s*,?\s*(\d{1,2}:\d{2})", text)
    if not m:
        return None
    iso = _parse_ru_date(m.group(1))
    if not iso:
        return None
    hall_m = re.search(r"Зал[^,\n]*", text)
    return {
        "date": iso,
        "time": m.group(2),
        "hall": hall_m.group(0).strip() if hall_m else "",
        "type": "Судебное заседание",
    }


@dataclass(frozen=True)
class KadRecord:
    case_number: str
    status: str
    category: str
    court: str
    judge: str
    plaintiffs: tuple[str, ...]
    respondents: tuple[str, ...]
    third_parties: tuple[str, ...]
    other_parties: tuple[str, ...]
    next_hearing_raw: str
    duration: str
    decision_date: str
    instance_id: str
    source: str
    source_url: str
    retrieved_at: str
    raw_artifact_hash: str
    events: tuple[dict[str, Any], ...] = ()
    instances: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    instance_level: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "case_number": self.case_number,
            "status": self.status,
            "category": self.category,
            "court": self.court,
            "judge": self.judge,
            "plaintiffs": list(self.plaintiffs),
            "respondents": list(self.respondents),
            "third_parties": list(self.third_parties),
            "other_parties": list(self.other_parties),
            "next_hearing_raw": self.next_hearing_raw,
            "duration": self.duration,
            "decision_date": self.decision_date,
            "instance_id": self.instance_id,
            "events": [dict(e) for e in self.events],
            "instances": [dict(i) for i in self.instances],
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "parser_version": KAD_PARSER_VERSION,
            "raw_artifact_hash": self.raw_artifact_hash,
            "warnings": list(self.warnings),
            "instance_level": self.instance_level,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True,
                          ensure_ascii=False, indent=2)

    def to_case_snapshot(self) -> CaseSnapshot:
        hearing = _parse_hearing(self.next_hearing_raw)
        events = []
        for e in self.events:
            iso = _parse_ru_date(e.get("date", ""))
            name = e.get("result") or e.get("type") or "Событие"
            if iso:
                events.append({"date": iso, "name": name,
                               "published": bool(e.get("published")),
                               "pdf_url": e.get("pdf", "")})
        if self.decision_date and not events:
            events.append({"date": self.decision_date,
                           "name": "Дата поступления/решения",
                           "published": False})
        payload = {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge or "unknown",
            "status": self.status,
            "participants": {
                "plaintiff": list(self.plaintiffs),
                "respondent": list(self.respondents),
                "third_parties": list(self.third_parties),
            },
            "next_hearing": hearing,
            "last_event": None,
            "events_timeline": events,
            "updated_at": (hearing["date"] if hearing
                           else self.decision_date or ""),
        }
        return normalize(payload, source=self.source,
                         source_url=self.source_url,
                         retrieved_at=self.retrieved_at,
                         raw_artifact_hash=self.raw_artifact_hash,
                         warnings=self.warnings,
                         parser_version=KAD_PARSER_VERSION)


def detect_kad_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in CHALLENGE_MARKERS)


def parse_kad_data(data: dict[str, Any], *, source: str, source_url: str,
                   retrieved_at: str, raw_artifact_hash: str) -> KadRecord:
    """Parse extracted KAD data dict into a KadRecord. Fail-closed."""
    if not isinstance(data, dict):
        raise MalformedPayloadError(source, "KAD data must be a dict")

    full_text = data.get("full_text", "")
    if detect_kad_challenge(full_text):
        raise WafDetectedError(source, 200)

    case_number = (data.get("case_number") or "").strip()
    if not case_number or not CASE_NUM_RE.search(case_number):
        raise MalformedPayloadError(
            source, f"missing/invalid case_number: '{case_number}'")

    status = (data.get("status") or "").strip()
    if not status:
        raise MalformedPayloadError(source, "missing status")

    court = (data.get("court") or "").strip()
    if not court:
        raise MalformedPayloadError(source, "missing court")

    def _clean_list(val: Any) -> tuple[str, ...]:
        if not isinstance(val, list):
            return ()
        return tuple(x.strip() for x in val if isinstance(x, str) and x.strip())

    warnings = []
    if not data.get("judge"):
        warnings.append("judge not extracted from KAD card")
    if not data.get("instance_id"):
        warnings.append("instance_id missing (AJAX tabs may not have loaded)")

    instance_level = detect_instance_level(case_number, data)

    return KadRecord(
        case_number=case_number,
        status=status,
        category=(data.get("category") or "").strip(),
        court=court,
        judge=(data.get("judge") or "").strip(),
        plaintiffs=_clean_list(data.get("plaintiffs")),
        respondents=_clean_list(data.get("respondents")),
        third_parties=_clean_list(data.get("third_parties")),
        other_parties=_clean_list(data.get("other")),
        next_hearing_raw=(data.get("next_hearing") or "").strip(),
        duration=(data.get("duration") or "").strip(),
        decision_date=(data.get("decision_date") or "").strip(),
        instance_id=(data.get("instance_id") or "").strip(),
        events=tuple(dict(e) for e in (data.get("events") or [])
                     if isinstance(e, dict)),
        instances=tuple(dict(i) for i in (data.get("instances") or [])
                        if isinstance(i, dict)),
        source=source,
        source_url=source_url,
        retrieved_at=retrieved_at,
        raw_artifact_hash=raw_artifact_hash,
        warnings=tuple(warnings),
        instance_level=instance_level,
    )


def fetch_kad_html(provider: Provider, case_ref: str,
                   retrieved_at: str) -> FetchOutcome:
    """lookup -> classify -> parse -> FetchOutcome (mirrors mos pipeline)."""
    from ..pipeline import DEGRADED, ERROR, OK, FetchOutcome
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    if raw.media_type != "text/html":
        return FetchOutcome(ERROR, None, "KAD pipeline requires text/html")
    try:
        # For KAD, data comes from browser evaluate, not raw HTML parsing.
        # The provider must supply the extracted data dict via metadata.
        extracted = getattr(raw, "metadata", {}).get("kad_data")
        if not extracted:
            raise MalformedPayloadError(raw.source,
                                        "no KAD extracted data in metadata")
        record = parse_kad_data(extracted, source=raw.source,
                                source_url=raw.source_url,
                                retrieved_at=retrieved_at,
                                raw_artifact_hash=compute_raw_hash(raw.body))
        if not refs_match(record.case_number, case_ref):
            raise MalformedPayloadError(
                raw.source, f"case identity mismatch: requested "
                            f"'{case_ref}', card reports "
                            f"'{record.case_number}'")
        snapshot = record.to_case_snapshot()
    except WafDetectedError as exc:
        return FetchOutcome(DEGRADED, None, str(exc), exc.retry_after)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    return FetchOutcome(OK, snapshot, "normalized")


__all__ = ["KadRecord", "parse_kad_data", "fetch_kad_html",
           "detect_kad_challenge", "EXTRACT_EXPRESSION",
           "KAD_PARSER_VERSION"]
