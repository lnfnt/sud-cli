"""Live source adapters: NOT IMPLEMENTED / QUARANTINE.

These stubs exist so wiring and CLI can report an explicit QUARANTINE status.
They perform no network I/O. Real adapters land in later phases behind
explicit human gates.
"""

from __future__ import annotations

from .base import Provider, RawSourceResult
from ..errors import SourceUnavailableError


class _QuarantinedProvider(Provider):
    reason = "QUARANTINE"

    def lookup(self, case_ref: str) -> RawSourceResult:
        raise SourceUnavailableError(self.name, self.reason)


class KadProvider(_QuarantinedProvider):
    name = "kad"
    reason = "NOT_IMPLEMENTED: phase-4 gated (browser spike, no stealth)"


class MosGorsudProvider(_QuarantinedProvider):
    name = "mos-gorsud"
    reason = ("NOT_IMPLEMENTED: live browser/API fetch QUARANTINE (phase-3 "
              "gate); replay-only DOM parser lives in case_search.mos")


class SudrfProvider(_QuarantinedProvider):
    name = "sudrf"
    reason = "NOT_IMPLEMENTED: future phase"
