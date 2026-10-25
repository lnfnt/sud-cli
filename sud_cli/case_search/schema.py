"""Canonical CaseSnapshot schema with provenance.

Serialization is deterministic: `to_dict` yields a stable key order and
`to_json` uses sort_keys so a snapshot always hashes to the same value for the
same logical content. Provenance is mandatory on every snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import PARSER_VERSION


def _canonical_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Provenance:
    source: str
    source_url: str
    retrieved_at: str
    parser_version: str = PARSER_VERSION
    raw_artifact_hash: str = ""
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for fname in ("source", "source_url", "retrieved_at",
                      "parser_version", "raw_artifact_hash"):
            value = getattr(self, fname)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"provenance.{fname} must be a non-empty string")
        if not isinstance(self.warnings, tuple):
            raise ValueError("provenance.warnings must be a tuple")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "parser_version": self.parser_version,
            "raw_artifact_hash": self.raw_artifact_hash,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Hearing:
    date: str
    time: str
    hall: str
    type: str

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "time": self.time, "hall": self.hall,
                "type": self.type}


@dataclass(frozen=True)
class LastEvent:
    date: str
    event_type: str
    document_url: str

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "event_type": self.event_type,
                "document_url": self.document_url}


@dataclass(frozen=True)
class TimelineEvent:
    date: str
    name: str
    published: bool
    # Optional document link (KAD PdfDocument URL). Empty for sources
    # without per-event documents; excluded from diff (date+name+pub
    # remain the change key).
    pdf_url: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.pdf_url, str):
            raise ValueError(
                "TimelineEvent.pdf_url must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "name": self.name,
                "published": self.published, "pdf_url": self.pdf_url}


@dataclass(frozen=True)
class Parties:
    plaintiff: tuple[str, ...] = ()
    respondent: tuple[str, ...] = ()
    third_parties: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plaintiff": list(self.plaintiff),
            "respondent": list(self.respondent),
            "third_parties": list(self.third_parties),
        }


@dataclass(frozen=True)
class CaseSnapshot:
    case_number: str
    court: str
    judge: str
    status: str
    parties: Parties
    next_hearing: Hearing | None
    last_event: LastEvent | None
    events_timeline: tuple[TimelineEvent, ...]
    updated_at: str
    provenance: Provenance

    def __post_init__(self) -> None:
        for fname in ("case_number", "court", "status", "updated_at"):
            value = getattr(self, fname)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"CaseSnapshot.{fname} must be a non-empty string")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("CaseSnapshot.provenance must be a Provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge,
            "status": self.status,
            "parties": self.parties.to_dict(),
            "next_hearing": self.next_hearing.to_dict()
            if self.next_hearing else None,
            "last_event": self.last_event.to_dict()
            if self.last_event else None,
            "events_timeline": [e.to_dict() for e in self.events_timeline],
            "updated_at": self.updated_at,
            "provenance": self.provenance.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2)

    def content_hash(self) -> str:
        return _canonical_hash(self.to_dict())


def compute_raw_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
