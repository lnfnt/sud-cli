"""Deterministic normalization of raw payloads into CaseSnapshot.

Fails closed: any missing/extra-shaped required field raises
MalformedPayloadError instead of producing a partially-filled snapshot.
"""

from __future__ import annotations

from typing import Any

from . import PARSER_VERSION
from .errors import MalformedPayloadError
from .schema import (CaseSnapshot, Hearing, LastEvent, Parties, Provenance,
                     TimelineEvent)

REQUIRED_TOP = ("case_number", "court", "status")


def _need(mapping: Any, key: str, source: str, ctx: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise MalformedPayloadError(source, f"missing field '{ctx}{key}'")
    return mapping[key]


def _str(mapping: dict, key: str, source: str, ctx: str = "") -> str:
    value = _need(mapping, key, source, ctx)
    if not isinstance(value, str) or not value.strip():
        raise MalformedPayloadError(
            source, f"field '{ctx}{key}' must be a non-empty string")
    return value


def _str_list(value: Any, source: str, ctx: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str)
                                          for v in value):
        raise MalformedPayloadError(
            source, f"field '{ctx}' must be a list of strings")
    return tuple(value)


def _parse_hearing(raw: Any, source: str) -> Hearing | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MalformedPayloadError(source, "'next_hearing' must be object")
    return Hearing(
        date=_str(raw, "date", source, "next_hearing."),
        time=_str(raw, "time", source, "next_hearing."),
        hall=_str(raw, "hall", source, "next_hearing."),
        type=_str(raw, "type", source, "next_hearing."),
    )


def _parse_last_event(raw: Any, source: str) -> LastEvent | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MalformedPayloadError(source, "'last_event' must be object")
    return LastEvent(
        date=_str(raw, "date", source, "last_event."),
        event_type=_str(raw, "event_type", source, "last_event."),
        document_url=_str(raw, "document_url", source, "last_event."),
    )


def _parse_timeline(raw: Any, source: str) -> tuple[TimelineEvent, ...]:
    if not isinstance(raw, list):
        raise MalformedPayloadError(
            source, "'events_timeline' must be a list")
    events: list[TimelineEvent] = []
    seen: set[tuple[str, str]] = set()
    for idx, item in enumerate(raw):
        ctx = f"events_timeline[{idx}]."
        pdf_url = item.get("pdf_url", "")
        if not isinstance(pdf_url, str):
            raise MalformedPayloadError(
                source, f"field '{ctx}pdf_url' must be a string")
        events.append(TimelineEvent(
            date=_str(item, "date", source, ctx),
            name=_str(item, "name", source, ctx),
            published=_need(item, "published", source, ctx),
            pdf_url=pdf_url,
        ))
        if not isinstance(events[-1].published, bool):
            raise MalformedPayloadError(
                source, f"field '{ctx}published' must be boolean")
        key = (events[-1].date, events[-1].name)
        if key in seen:
            raise MalformedPayloadError(
                source, f"duplicate timeline event {key}")
        seen.add(key)
    return tuple(events)


def normalize(payload: dict[str, Any], *, source: str, source_url: str,
              retrieved_at: str, raw_artifact_hash: str,
              warnings: tuple[str, ...] = (),
              parser_version: str = PARSER_VERSION) -> CaseSnapshot:
    """Convert a parsed synthetic payload into a canonical snapshot."""
    if not isinstance(payload, dict):
        raise MalformedPayloadError(source, "payload must be a JSON object")

    missing = [k for k in REQUIRED_TOP if k not in payload]
    if missing:
        raise MalformedPayloadError(
            source, f"missing required fields: {sorted(missing)}")

    parties_raw = payload.get("participants", {})
    if not isinstance(parties_raw, dict):
        raise MalformedPayloadError(source, "'participants' must be object")
    parties = Parties(
        plaintiff=_str_list(parties_raw.get("plaintiff", []), source,
                            "participants.plaintiff"),
        respondent=_str_list(parties_raw.get("respondent", []), source,
                             "participants.respondent"),
        third_parties=_str_list(parties_raw.get("third_parties", []), source,
                                "participants.third_parties"),
    )

    provenance = Provenance(
        source=source,
        source_url=source_url,
        retrieved_at=retrieved_at,
        parser_version=parser_version,
        raw_artifact_hash=raw_artifact_hash,
        warnings=warnings,
    )

    return CaseSnapshot(
        case_number=_str(payload, "case_number", source),
        court=_str(payload, "court", source),
        judge=_str(payload, "judge", source)
        if payload.get("judge") is not None else "unknown",
        status=_str(payload, "status", source),
        parties=parties,
        next_hearing=_parse_hearing(payload.get("next_hearing"), source),
        last_event=_parse_last_event(payload.get("last_event"), source),
        events_timeline=_parse_timeline(
            payload.get("events_timeline", []), source),
        updated_at=_str(payload, "updated_at", source),
        provenance=provenance,
    )
