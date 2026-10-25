"""Provider contract and fail-closed classification of raw results.

`lookup(case_ref) -> RawSourceResult` per spec. Classification NEVER turns a
WAF/captcha/empty/malformed response into a successful payload.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import (CaseSearchError, EmptyPayloadError, HttpError,
                      MalformedPayloadError, SourceUnavailableError,
                      WafDetectedError)

WAF_MARKERS = ("captcha", "challenge", "access denied", "forbidden",
               "cf-browser-verification")
DEGRADED_STATUSES = frozenset({403, 406, 429, 503})


@dataclass(frozen=True)
class RawSourceResult:
    source: str
    source_url: str
    status_code: int
    body: str
    error: str | None = None
    retry_after: float | None = None
    media_type: str = "application/json"
    # Optional provider-supplied payload (e.g. browser-extracted KAD data
    # dict alongside the raw body). None for plain HTTP results.
    metadata: dict | None = None


class Provider(ABC):
    """Case lookup provider. Implementations must not hit real networks."""

    name: str = "abstract"
    media_type: str = "application/json"
    accepts_fixtures: bool = False

    @abstractmethod
    def lookup(self, case_ref: str) -> RawSourceResult:
        """Return the raw result for a case reference."""


@dataclass(frozen=True)
class Classified:
    """Result of fail-closed classification of a RawSourceResult."""

    ok: bool
    degraded: bool
    payload: dict | None
    reason: str | None
    retry_after: float | None


def classify(result: RawSourceResult) -> Classified:
    """Classify a raw result without ever fabricating an empty success."""
    if result.media_type != "application/json":
        raise MalformedPayloadError(
            result.source,
            f"media type '{result.media_type}' must not enter the JSON "
            "classifier; use its own parser pipeline")
    lowered_body = result.body.lower()
    retry_after = result.retry_after if (
        result.retry_after is not None and result.retry_after > 0) else None

    if (result.error and result.error in ("captcha_or_waf",)) or any(
            marker in lowered_body for marker in WAF_MARKERS):
        raise WafDetectedError(result.source, result.status_code,
                               retry_after)

    if result.status_code in DEGRADED_STATUSES:
        raise WafDetectedError(result.source, result.status_code,
                               retry_after)

    if result.status_code >= 400:
        raise HttpError(result.status_code, f"from '{result.source}'")

    if not result.body.strip():
        raise EmptyPayloadError(result.source)

    try:
        payload = json.loads(result.body)
    except json.JSONDecodeError as exc:
        raise MalformedPayloadError(result.source, str(exc)) from exc

    if payload is None:
        raise EmptyPayloadError(result.source)

    return Classified(ok=True, degraded=False, payload=payload,
                      reason=None, retry_after=None)


__all__ = ["Provider", "RawSourceResult", "Classified", "classify",
           "CaseSearchError", "SourceUnavailableError"]
