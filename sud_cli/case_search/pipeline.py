"""Fetch pipeline: lookup -> classify -> normalize, with fail-closed outcomes.

An outcome is one of:
- ok: snapshot produced;
- degraded: WAF/captcha/rate-limit; no snapshot fabricated, retry_after kept;
- error: empty/malformed/http failure.

A degraded/error outcome NEVER carries a snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CaseSearchError, WafDetectedError
from .normalize import normalize
from .schema import CaseSnapshot, compute_raw_hash
from .sources.base import Provider, classify

DEGRADED = "degraded"
ERROR = "error"
OK = "ok"


@dataclass(frozen=True)
class FetchOutcome:
    status: str
    snapshot: CaseSnapshot | None
    reason: str
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK


def fetch(provider: Provider, case_ref: str,
          retrieved_at: str) -> FetchOutcome:
    """Run one fail-closed lookup against a provider."""
    if getattr(provider, "media_type", "application/json") == "text/html":
        from .mos.parser import fetch_mos_html
        return fetch_mos_html(provider, case_ref, retrieved_at)
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))

    try:
        classified = classify(raw)
    except WafDetectedError as exc:
        return FetchOutcome(DEGRADED, None, str(exc), exc.retry_after)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))

    if classified.payload is None:
        return FetchOutcome(ERROR, None, "classifier produced no payload")
    try:
        snapshot = normalize(
            classified.payload,
            source=raw.source,
            source_url=raw.source_url,
            retrieved_at=retrieved_at,
            raw_artifact_hash=compute_raw_hash(raw.body),
        )
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))

    return FetchOutcome(OK, snapshot, "normalized")
