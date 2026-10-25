"""Replay-only DOM parser for the redacted mos-gorsud case card.

STD-ONLY (`html.parser` via `case_search.mos.dom`). This module performs no
network I/O, imports no HTTP client and never dereferences live URLs: it
parses an HTML string the caller already holds (a saved, PII-redacted
fixture). A future browser adapter only has to hand the same
`RawSourceResult(media_type="text/html")` to `classify_html` +
`parse_mos_html`; until then the live adapter stays QUARANTINE
(`case_search.sources.live.MosGorsudProvider`).

Fail-closed guarantees: captcha/WAF/interstitial HTML, empty HTML, missing
required case summary, malformed table structure and ambiguous duplicate
fields all raise — they never yield a partially-parsed or empty success.

PII policy: the "Стороны" (parties) cell is detected but NEVER parsed or
emitted, so participant names (including partially-redacted ones inside the
fixture) cannot leak into snapshots, records, alerts or stored artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .dom import (Node, anchors, build_tree, collapse, find_all, find_first,
                  iter_nodes, text_of)
from ..errors import (CaseSearchError, EmptyPayloadError, HttpError,
                      MalformedPayloadError, WafDetectedError)
from ..normalize import normalize
from ..schema import CaseSnapshot, Provenance, compute_raw_hash
from ..sources.base import DEGRADED_STATUSES, Provider, RawSourceResult

MOS_DOM_PARSER_VERSION = "mos-dom-replay-0.1.0"

CHALLENGE_MARKERS = (
    "cf-browser-verification", "captcha challenge", "access denied",
    "доступ ограничен", "подтвердите, что вы не робот",
    "attention required! | cloudflare",
)

H1_CASE_RE = re.compile(r"Информация по делу\s*№\s*(\S+)")
CASE_NUMBER_RE = re.compile(r"[0-9А-ЯA-Z]{1,4}-[0-9]{3,6}/20[0-9]{2}")
UID_RE = re.compile(r"\d{2}[A-Z]{2}\d{4}(?:-\d{2,6}){2,4}")
DOC_LINK_RE = re.compile(r"/cases/docs/content/[0-9A-Za-z-]{8,}")
ISO_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# Latin/Cyrillic lookalike fold: the real portal mixes alphabets in labels
# (e.g. "Cудья" with a Latin C), so label matching is done on folded text.
_LOOKALIKE = str.maketrans({
    "c": "с", "a": "а", "e": "е", "o": "о", "x": "х", "y": "у", "p": "р",
    "k": "к", "m": "м", "t": "т",
})

SUMMARY_UID = "уникальный идентификатор дела"
SUMMARY_NUMBERS = "номер дела ~ материала"
SUMMARY_JUDGE = "судья"
SUMMARY_STATUS = "текущее состояние"
SUMMARY_CATEGORY = "категория дела"
SUMMARY_FILED = "дата поступления"
SUMMARY_FI_DATE = "дата рассмотрения дела в первой инстанции"
SUMMARY_FI_RESULT = "решение первой инстанции"
SUMMARY_APPEAL_NUMBERS = "номер дела в суде вышестоящей инстанции"
SUMMARY_APPEAL_RESULT = "решение апелляции"
SUMMARY_EFFECTIVE = "дата вступления решения в силу"
SUMMARY_PARTIES = "стороны"
REQUIRED_SUMMARY = (SUMMARY_UID, SUMMARY_NUMBERS, SUMMARY_JUDGE,
                    SUMMARY_STATUS)

SECTION_TIMELINE = "движение дела"
SECTION_HEARINGS = "судебные заседания"
SECTION_DOCUMENTS = "документы"

_HEARING_FIELDS = ("время", "зал", "стадия", "результат", "основание")
_HEARING_AUDIO = "использовалось аудио/видео протоколирование"

_PARTIES_SENTINEL = "законного представителя"


def fold_label(text: str) -> str:
    return collapse(text).casefold().translate(_LOOKALIKE)


# ---------------------------------------------------------------------------
# records


@dataclass(frozen=True)
class MosTimelineEvent:
    date: str
    state: str
    document: str

    def to_dict(self) -> dict:
        return {"date": self.date, "state": self.state,
                "document": self.document}


@dataclass(frozen=True)
class MosHearing:
    date: str
    time: str
    hall: str
    stage: str
    result: str
    grounds: str
    recording: str

    def to_dict(self) -> dict:
        return {"date": self.date, "time": self.time, "hall": self.hall,
                "stage": self.stage, "result": self.result,
                "grounds": self.grounds, "recording": self.recording}


@dataclass(frozen=True)
class MosDocument:
    date: str
    kind: str
    link: str
    published: bool
    note: str

    def to_dict(self) -> dict:
        return {"date": self.date, "kind": self.kind, "link": self.link,
                "published": self.published, "note": self.note}


@dataclass(frozen=True)
class MosAppealRef:
    number: str
    url: str

    def to_dict(self) -> dict:
        return {"number": self.number, "url": self.url}


@dataclass(frozen=True)
class MosRecord:
    """Structured replay result. Deliberately has no parties field: the
    parties cell is never parsed (PII policy)."""

    case_number: str
    case_uid: str
    court: str
    judge: str
    status: str
    category: str
    filed_date: str
    first_instance_date: str
    first_instance_result: str
    appeal_result: str
    effective_date: str
    related_numbers: tuple[str, ...]
    appeal_numbers: tuple[MosAppealRef, ...]
    timeline: tuple[MosTimelineEvent, ...]
    hearings: tuple[MosHearing, ...]
    documents: tuple[MosDocument, ...]
    source: str
    source_url: str
    retrieved_at: str
    raw_artifact_hash: str
    warnings: tuple[str, ...] = ()
    _snapshot_cache: CaseSnapshot | None = field(default=None, compare=False,
                                                 repr=False)

    def to_dict(self) -> dict:
        return {
            "case_number": self.case_number,
            "case_uid": self.case_uid,
            "court": self.court,
            "judge": self.judge,
            "status": self.status,
            "category": self.category,
            "filed_date": self.filed_date,
            "first_instance_date": self.first_instance_date,
            "first_instance_result": self.first_instance_result,
            "appeal_result": self.appeal_result,
            "effective_date": self.effective_date,
            "related_numbers": list(self.related_numbers),
            "appeal_numbers": [a.to_dict() for a in self.appeal_numbers],
            "timeline": [e.to_dict() for e in self.timeline],
            "hearings": [h.to_dict() for h in self.hearings],
            "documents": [d.to_dict() for d in self.documents],
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "parser_version": MOS_DOM_PARSER_VERSION,
            "raw_artifact_hash": self.raw_artifact_hash,
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2)

    def content_hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True,
                          ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def to_case_snapshot(self) -> CaseSnapshot:
        """Map onto the canonical CaseSnapshot via `normalize` (validation is
        shared, not duplicated). next_hearing/last_event stay None: replay
        has no wall clock and fabricating a "next" hearing would be wrong."""
        published_docs = {(d.date, d.kind) for d in self.documents
                          if d.link}
        payload = {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge,
            "status": self.status,
            "participants": {},
            "next_hearing": None,
            "last_event": None,
            "events_timeline": [
                {"date": e.date, "name": e.state,
                 "published": (e.date, e.document) in published_docs}
                for e in self.timeline
            ],
            "updated_at": self.timeline[-1].date if self.timeline else "",
        }
        return normalize(payload, source=self.source,
                         source_url=self.source_url,
                         retrieved_at=self.retrieved_at,
                         raw_artifact_hash=self.raw_artifact_hash,
                         warnings=self.warnings,
                         parser_version=MOS_DOM_PARSER_VERSION)


# ---------------------------------------------------------------------------
# small validators


def _malformed(source: str, detail: str) -> MalformedPayloadError:
    return MalformedPayloadError(source, detail)


def _iso_date(source: str, text: str, ctx: str) -> str:
    m = ISO_DATE_RE.fullmatch(text)
    if not m:
        raise _malformed(source, f"{ctx}: invalid date '{text}'")
    day, month, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        raise _malformed(source, f"{ctx}: impossible date '{text}'") from None


def _opt_iso_date(source: str, text: str, ctx: str) -> str:
    if not text:
        return ""
    if not ISO_DATE_RE.search(text):
        raise _malformed(source, f"{ctx}: invalid date '{text}'")
    m = ISO_DATE_RE.search(text)
    return _iso_date(source, m.group(0), ctx)


def _hhmm(source: str, text: str, ctx: str) -> str:
    m = TIME_RE.match(text)
    if not m:
        raise _malformed(source, f"{ctx}: invalid time '{text}'")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise _malformed(source, f"{ctx}: impossible time '{text}'")
    return f"{hour:02d}:{minute:02d}"


# ---------------------------------------------------------------------------
# classification (fail-closed, before any structural parsing)


def detect_challenge(root: Node) -> bool:
    """Captcha/WAF/interstitial detection on VISIBLE text only.

    Script content is deliberately excluded: the healthy mos-gorsud card
    embeds smart-captcha *script paths* and would false-positive on a naive
    substring scan of the raw HTML.
    """
    title = find_first(root, tag="title")
    hay = "\n".join([text_of(title) if title else "", text_of(root)])
    lowered = hay.casefold()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def classify_html(result: RawSourceResult) -> None:
    """Fail-closed classification of an HTML `RawSourceResult`.

    Returns None only when the payload deserves a structural parse. WAF /
    captcha / degraded status / empty body always raise.
    """
    retry_after = result.retry_after if (
        result.retry_after is not None and result.retry_after > 0) else None
    if result.error == "captcha_or_waf":
        raise WafDetectedError(result.source, result.status_code, retry_after)
    if not result.body.strip():
        raise EmptyPayloadError(result.source)
    root = build_tree(result.body)
    if detect_challenge(root):
        raise WafDetectedError(result.source, result.status_code, retry_after)
    if result.status_code in DEGRADED_STATUSES:
        raise WafDetectedError(result.source, result.status_code, retry_after)
    if result.status_code >= 400:
        raise HttpError(result.status_code, f"from '{result.source}'")


# ---------------------------------------------------------------------------
# parsing


def _summary_rows(source: str, root: Node) -> dict[str, tuple[str, Node]]:
    anti = find_first(root, css_class="anti-container")
    if anti is None:
        raise _malformed(source, "missing required case summary: no case "
                                 "card container (.anti-container)")
    rows: dict[str, tuple[str, Node]] = {}
    for row in find_all(anti, tag="div", css_class="row"):
        left = find_first(row, css_class="left")
        right = find_first(row, css_class="right")
        if left is None or right is None:
            raise _malformed(source, "malformed summary row: missing "
                                      "label or value cell")
        label = fold_label(text_of(left))
        value = text_of(right)
        if label in rows:
            prev_value, _ = rows[label]
            if prev_value != value:
                raise _malformed(
                    source,
                    f"ambiguous duplicate summary field '{label}'")
            continue
        rows[label] = (value, right)
    missing = sorted(label for label in REQUIRED_SUMMARY if label not in rows)
    if missing:
        raise _malformed(source,
                         f"missing required case summary fields: {missing}")
    for label in REQUIRED_SUMMARY:
        if not rows[label][0]:
            raise _malformed(source, f"required summary field '{label}' "
                                     "is empty")
    return rows


def _summary_text(summary: dict, label: str) -> str:
    return summary.get(label, ("", None))[0]


def _summary_node(summary: dict, label: str) -> Node | None:
    return summary.get(label, ("", None))[1]


def _group_sections(source: str, root: Node,
                    warnings: list[str]) -> dict[str, list]:
    sections: dict[str, list] = {}
    for group in find_all(root, tag="div", css_class="group"):
        header = find_first(group, tag="h3")
        name = fold_label(text_of(header)) if header else ""
        if name not in (SECTION_TIMELINE, SECTION_HEARINGS, SECTION_DOCUMENTS):
            if name:
                warnings.append(f"ignoring unknown section '{name}'")
            continue
        if name in sections:
            raise _malformed(source, f"ambiguous duplicate section "
                                     f"'{name}'")
        rows = []
        for tr in find_all(group, tag="tr"):
            cells = [c for c in tr.children
                     if isinstance(c, Node) and c.tag == "td"]
            if len(cells) != 2:
                raise _malformed(
                    source,
                    f"malformed '{name}' table structure: row has "
                    f"{len(cells)} cells, expected 2")
            rows.append((fold_label(text_of(cells[0])), text_of(cells[1]),
                         anchors(cells[1])))
        sections[name] = rows
    for required in (SECTION_TIMELINE,):
        if required not in sections:
            raise _malformed(source, f"missing required section "
                                     f"'{required}'")
    return sections


def _parse_timeline(source: str, rows: list) -> tuple[MosTimelineEvent, ...]:
    events: list[MosTimelineEvent] = []
    cur: dict | None = None
    for idx, (label, value, _links) in enumerate(rows):
        if label == "дата":
            if cur is not None:
                raise _malformed(
                    source, f"malformed timeline structure at row {idx}: "
                            "'Дата' before previous event closed")
            cur = {"date": _iso_date(source, value, f"timeline[{idx}]")}
        elif label == "состояние":
            if cur is None:
                raise _malformed(source, f"malformed timeline structure at "
                                         f"row {idx}: 'Состояние' without "
                                         "'Дата'")
            if "state" in cur:
                raise _malformed(source, f"ambiguous duplicate timeline "
                                         f"field 'Состояние' at row {idx}")
            if not value:
                raise _malformed(source, f"empty timeline 'Состояние' at "
                                         f"row {idx}")
            cur["state"] = value
        elif label == "документ-основание":
            if cur is None or "state" not in cur:
                raise _malformed(source, f"malformed timeline structure at "
                                         f"row {idx}: 'Документ-основание' "
                                         "out of sequence")
            events.append(MosTimelineEvent(date=cur["date"],
                                           state=cur["state"],
                                           document=value))
            cur = None
        else:
            raise _malformed(source, f"unknown timeline field '{label}'")
    if cur is not None:
        raise _malformed(source, "malformed timeline structure: unterminated "
                                 "final event")
    if not events:
        raise _malformed(source, "missing required case movement timeline")
    return tuple(events)


def _parse_hearings(source: str, rows: list) -> tuple[MosHearing, ...]:
    hearings: list[MosHearing] = []
    cur: dict | None = None
    got: list[str] = []
    for idx, (label, value, _links) in enumerate(rows):
        if label == "дата":
            if cur is not None:
                raise _malformed(source, f"malformed hearings structure at "
                                         f"row {idx}: hearing not closed "
                                         "before next 'Дата'")
            cur = {"date": _iso_date(source, value, f"hearing[{idx}]")}
        elif label == _HEARING_AUDIO:
            if cur is None:
                raise _malformed(source, f"malformed hearings structure at "
                                         f"row {idx}: audio row without "
                                         "'Дата'")
            missing = [f for f in _HEARING_FIELDS if f not in got]
            if missing:
                raise _malformed(source, f"malformed hearings structure: "
                                         f"incomplete hearing, missing "
                                         f"{missing}")
            hearings.append(MosHearing(
                date=cur["date"], time=cur["time"], hall=cur.get("зал", ""),
                stage=cur.get("стадия", ""), result=cur.get("результат", ""),
                grounds=cur.get("основание", ""), recording=value))
            cur, got = None, []
        elif label in _HEARING_FIELDS:
            if cur is None:
                raise _malformed(source, f"malformed hearings structure at "
                                         f"row {idx}: '{label}' without "
                                         "'Дата'")
            if label in got:
                raise _malformed(source, f"ambiguous duplicate hearing "
                                         f"field '{label}'")
            if _HEARING_FIELDS[len(got)] != label:
                raise _malformed(source, f"malformed hearings structure at "
                                         f"row {idx}: unexpected field "
                                         f"'{label}'")
            if label == "время":
                cur["time"] = _hhmm(source, value, f"hearing[{idx}]")
            else:
                cur[label] = value
            got.append(label)
        else:
            raise _malformed(source, f"unknown hearings field '{label}'")
    if cur is not None:
        raise _malformed(source, "malformed hearings structure: unterminated "
                                 "final hearing")
    return tuple(hearings)


def _parse_documents(source: str, rows: list) -> tuple[MosDocument, ...]:
    documents: list[MosDocument] = []
    cur: dict | None = None
    for idx, (label, value, links) in enumerate(rows):
        if label == "дата документа":
            if cur is not None:
                raise _malformed(source, f"malformed documents structure at "
                                         f"row {idx}: document not closed")
            cur = {"date": _iso_date(source, value, f"document[{idx}]")}
        elif label == "вид документа":
            if cur is None:
                raise _malformed(source, f"malformed documents structure at "
                                         f"row {idx}: 'Вид документа' "
                                         "without 'Дата документа'")
            if "kind" in cur:
                raise _malformed(source, f"ambiguous duplicate document "
                                         f"field 'Вид документа'")
            if not value:
                raise _malformed(source, f"empty document kind at row {idx}")
            cur["kind"] = value
        elif label == "текст документа":
            if cur is None or "kind" not in cur:
                raise _malformed(source, f"malformed documents structure at "
                                         f"row {idx}: 'Текст документа' out "
                                         "of sequence")
            doc_links = [href for _t, href in links
                         if DOC_LINK_RE.search(href)]
            other_links = [href for _t, href in links
                           if not DOC_LINK_RE.search(href)]
            if other_links:
                raise _malformed(source, f"unexpected document link "
                                         f"'{other_links[0]}'")
            if len(doc_links) > 1:
                raise _malformed(source, f"ambiguous duplicate document "
                                         f"links at row {idx}")
            link = doc_links[0] if doc_links else ""
            documents.append(MosDocument(date=cur["date"], kind=cur["kind"],
                                         link=link, published=bool(link),
                                         note="" if link else value))
            cur = None
        else:
            raise _malformed(source, f"unknown documents field '{label}'")
    if cur is not None:
        raise _malformed(source, "malformed documents structure: unterminated "
                                 "final document")
    return tuple(documents)


def parse_mos_html(html: str, *, source: str, source_url: str,
                   retrieved_at: str) -> MosRecord:
    """Parse a saved mos-gorsud case-card HTML string (replay only).

    Fails closed on challenge pages, empty input, missing summary and any
    malformed/ambiguous structure. Never emits participant names.
    """
    if not html.strip():
        raise EmptyPayloadError(source)
    root = build_tree(html)
    if detect_challenge(root):
        raise WafDetectedError(source)

    h1_numbers = set()
    for h1 in find_all(root, tag="h1"):
        match = H1_CASE_RE.search(text_of(h1))
        if match:
            h1_numbers.add(match.group(1))
    if len(h1_numbers) > 1:
        raise MalformedPayloadError(
            source, f"ambiguous duplicate case titles: {sorted(h1_numbers)}")
    if not h1_numbers:
        raise MalformedPayloadError(
            source, "missing required case summary: no case card <h1>")
    title_number = h1_numbers.pop()

    warnings: list[str] = []
    summary = _summary_rows(source, root)
    if SUMMARY_PARTIES in summary:
        warnings.append("parties cell present in source but never parsed "
                        "(PII policy)")
    sections = _group_sections(source, root, warnings)

    numbers_text = _summary_text(summary, SUMMARY_NUMBERS)
    numbers = CASE_NUMBER_RE.findall(numbers_text)
    if not numbers:
        raise MalformedPayloadError(source, "no case number found in summary "
                                            "row 'Номер дела ~ материала'")
    main = numbers[0]
    if main != title_number:
        raise MalformedPayloadError(
            source, f"ambiguous case number: card title says '{title_number}'"
                    f", summary row says '{main}'")
    if numbers.count(main) > 1:
        raise MalformedPayloadError(
            source, f"ambiguous duplicate case number '{main}'")
    related = tuple(n for n in numbers[1:] if n != main)

    uid_text = _summary_text(summary, SUMMARY_UID)
    uid_match = UID_RE.search(uid_text)
    if not uid_match:
        raise MalformedPayloadError(source,
                                    "missing case UID in summary row")
    case_uid = uid_match.group(0)

    court_node = find_first(root, css_class="drop-title")
    court = text_of(court_node) if court_node else ""
    if not court:
        title_node = find_first(root, tag="title")
        for segment in (text_of(title_node).split("|") if title_node else []):
            if segment.strip().casefold().endswith("суд"):
                court = collapse(segment)
                break
    if not court:
        raise MalformedPayloadError(source, "missing court name in card")

    judge = _summary_text(summary, SUMMARY_JUDGE)
    status = _summary_text(summary, SUMMARY_STATUS)

    appeal_refs: list[MosAppealRef] = []
    appeal_node = _summary_node(summary, SUMMARY_APPEAL_NUMBERS)
    if appeal_node is not None:
        text_numbers = CASE_NUMBER_RE.findall(
            _summary_text(summary, SUMMARY_APPEAL_NUMBERS))
        anchor_nums: list[tuple[str, str]] = []
        for a_text, href in anchors(appeal_node):
            match = CASE_NUMBER_RE.search(a_text)
            if match:
                anchor_nums.append((match.group(0), href))
            elif a_text.strip():
                raise MalformedPayloadError(
                    source, f"unrecognized appeal link label '{a_text}'")
        anchor_set = {n for n, _ in anchor_nums}
        if len(anchor_set) != len(anchor_nums):
            raise MalformedPayloadError(source,
                                        "ambiguous duplicate appeal numbers")
        appeal_refs = [MosAppealRef(number=n, url=u)
                       for n, u in anchor_nums]
        for extra in text_numbers:
            if extra not in anchor_set:
                appeal_refs.append(MosAppealRef(number=extra, url=""))

    timeline = _parse_timeline(source, sections[SECTION_TIMELINE])
    hearings = _parse_hearings(
        source, sections.get(SECTION_HEARINGS, []))
    documents = _parse_documents(
        source, sections.get(SECTION_DOCUMENTS, []))
    if not hearings:
        warnings.append("hearings section is empty")
    if not documents:
        warnings.append("documents section is empty")

    return MosRecord(
        case_number=main,
        case_uid=case_uid,
        court=court,
        judge=judge,
        status=status,
        category=_summary_text(summary, SUMMARY_CATEGORY),
        filed_date=_opt_iso_date(source, _summary_text(summary, SUMMARY_FILED),
                                 "filed_date"),
        first_instance_date=_opt_iso_date(
            source, _summary_text(summary, SUMMARY_FI_DATE),
            "first_instance_date"),
        first_instance_result=_summary_text(summary, SUMMARY_FI_RESULT),
        appeal_result=_summary_text(summary, SUMMARY_APPEAL_RESULT),
        effective_date=_opt_iso_date(
            source, _summary_text(summary, SUMMARY_EFFECTIVE),
            "effective_date"),
        related_numbers=related,
        appeal_numbers=tuple(appeal_refs),
        timeline=timeline,
        hearings=hearings,
        documents=documents,
        source=source,
        source_url=source_url,
        retrieved_at=retrieved_at,
        raw_artifact_hash=compute_raw_hash(html),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# fetch boundary shared with the future live adapter


def fetch_mos_html(provider: Provider, case_ref: str,
                   retrieved_at: str):
    """lookup -> classify_html -> parse_mos_html -> canonical snapshot.

    Mirrors `case_search.pipeline.fetch` semantics for HTML sources:
    degraded (WAF/captcha) and error outcomes NEVER carry a snapshot, and
    the parsed card is identity-bound to the requested `case_ref` (a valid
    card of a different case is an error, never a success).
    """
    from ..pipeline import DEGRADED, ERROR, OK, FetchOutcome
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    if raw.media_type != "text/html":
        return FetchOutcome(ERROR, None,
                            "mos HTML pipeline requires a text/html payload")
    try:
        classify_html(raw)
        record = parse_mos_html(raw.body, source=raw.source,
                                source_url=raw.source_url,
                                retrieved_at=retrieved_at)
        if record.case_number != case_ref:
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


__all__ = ["MosRecord", "MosTimelineEvent", "MosHearing", "MosDocument",
           "MosAppealRef", "MOS_DOM_PARSER_VERSION", "classify_html",
           "detect_challenge", "parse_mos_html", "fetch_mos_html"]
