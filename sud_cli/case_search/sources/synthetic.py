"""Synthetic fixture provider. Reads only local fixture files, no network."""

from __future__ import annotations

from pathlib import Path

from .base import Provider, RawSourceResult
from ..errors import SourceUnavailableError

FIXTURE_MAP = {
    "A40-SYNTH-2026": "kad_case.json",
    "02-1234/2026": "mos_gorsud_case.json",
    "SYNTH-WAF": "waf_response.json",
    "SYNTH-EMPTY": "empty_response.json",
    "SYNTH-MALFORMED": "malformed_response.json",
    "SYNTH-HTTP500": "http500_response.json",
    "SYNTH-BADSCHEMA": "malformed_case.json",
    "SYNTH-RATELIMIT": "ratelimit_response.json",
}


class SyntheticProvider(Provider):
    name = "synthetic"
    accepts_fixtures = True

    def __init__(self, fixtures_dir: Path | str):
        self.fixtures_dir = Path(fixtures_dir)

    def lookup(self, case_ref: str) -> RawSourceResult:
        filename = FIXTURE_MAP.get(case_ref)
        if filename is None:
            raise SourceUnavailableError(
                self.name, f"unknown synthetic case_ref '{case_ref}'")
        path = self.fixtures_dir / filename
        if not path.is_file():
            raise SourceUnavailableError(
                self.name, f"fixture missing: {filename}")
        body = path.read_text(encoding="utf-8")
        return RawSourceResult(
            source=f"synthetic:{filename}",
            source_url=f"file://{path.resolve()}",
            status_code=self._fixture_status(body),
            body=self._fixture_body(body),
            error=self._fixture_error(body),
            retry_after=self._fixture_retry_after(body),
        )

    @staticmethod
    def _wrap(body: str) -> dict | None:
        import json
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and "status_code" in data \
            else None

    @classmethod
    def _fixture_status(cls, body: str) -> int:
        wrapped = cls._wrap(body)
        return int(wrapped["status_code"]) if wrapped else 200

    @classmethod
    def _fixture_body(cls, body: str) -> str:
        wrapped = cls._wrap(body)
        if wrapped is None:
            return body
        raw = wrapped.get("body", "")
        return "" if raw is None else str(raw)

    @classmethod
    def _fixture_error(cls, body: str) -> str | None:
        wrapped = cls._wrap(body)
        return wrapped.get("error") if wrapped else None

    @classmethod
    def _fixture_retry_after(cls, body: str) -> float | None:
        wrapped = cls._wrap(body)
        value = wrapped.get("retry_after") if wrapped else None
        if value is None:
            return None
        try:
            retry_after = float(value)
        except (TypeError, ValueError):
            return None
        return retry_after if retry_after > 0 else None
