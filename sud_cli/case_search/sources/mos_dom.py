"""Replay-only mos-gorsud HTML fixture provider.

Reads saved, PII-redacted HTML from a local fixtures directory. Performs NO
network I/O of any kind. The `origin_url` entries below are recorded
provenance strings copied from `mos-spike/BROWSER-SPIKE-REPORT.md`; this
code never fetches them and the parser consumes only local bytes.
"""

from __future__ import annotations

from pathlib import Path

from .base import Provider, RawSourceResult
from ..errors import SourceUnavailableError

REPLAY_FIXTURES: dict[str, tuple[str, str]] = {
    "02-0387/2025": (
        "case-02-0387-2025.redacted.html",
        "https://m.mos-gorsud.ru/rs/hamovnicheskij/services/cases/civil/"
        "details/9ef63f11-5fa7-11ef-9d90-f3a74c864694"),
    "MOS-WAF": ("waf.html", "file://synthetic-interstitial"),
    "MOS-EMPTY": ("empty.html", "file://synthetic-empty"),
    "MOS-MISSING-SUMMARY": ("missing-summary.html",
                            "file://synthetic-no-card"),
    "MOS-MALFORMED": ("malformed-timeline.html",
                      "file://synthetic-malformed"),
    "MOS-AMBIGUOUS": ("ambiguous-summary.html",
                      "file://synthetic-ambiguous"),
}


class MosDomReplayProvider(Provider):
    """Local replay adapter: fixed case_ref -> saved redacted HTML."""

    name = "mos-gorsud-dom"
    media_type = "text/html"
    accepts_fixtures = True

    def __init__(self, fixtures_dir: Path | str):
        self.fixtures_dir = Path(fixtures_dir)

    def lookup(self, case_ref: str) -> RawSourceResult:
        entry = REPLAY_FIXTURES.get(case_ref)
        if entry is None:
            raise SourceUnavailableError(
                self.name, f"unknown replay case_ref '{case_ref}'")
        filename, origin_url = entry
        path = self.fixtures_dir / filename
        if not path.is_file():
            raise SourceUnavailableError(self.name,
                                         f"fixture missing: {filename}")
        body = path.read_text(encoding="utf-8")
        return RawSourceResult(
            source=f"{self.name}:{filename}",
            source_url=origin_url,
            status_code=200,
            body=body,
            media_type="text/html",
        )


__all__ = ["MosDomReplayProvider", "REPLAY_FIXTURES"]
