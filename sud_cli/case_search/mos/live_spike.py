"""Bounded LIVE browser adapter BOUNDARY for the mos-gorsud spike.

This module is an interface, not a browser runtime. It imports NO network,
browser or subprocess library (no playwright/selenium/browser-use/requests/
urllib/socket/subprocess): the actual navigation is performed by an INJECTED
browser client that must satisfy the small `BrowserClient` protocol (or be a
plain callable `(url, timeout_ms) -> BrowserPage`). Tests fake that boundary;
no code or test here can reach the network.

Hard scope (per `LIVE-ADAPTER-PLAN.md` / `LIVE-BROWSER-WORKER-PROMPT.md`):
- allowlist exactly one approved public case ref + its recorded card URL;
  unknown refs fail closed BEFORE any navigation is attempted;
- one navigation per lookup, timeout clamped to `MAX_TIMEOUT_MS`, no retries
  and no second navigation for the same lookup;
- no forms, no downloads, no cookie/auth persistence, no filesystem writes
  at all (so nothing can land outside a caller-provided scratch directory),
  no case enumeration;
- a `RawSourceResult(media_type="text/html")` is returned ONLY for a
  non-empty body carrying a visible case-card marker on an expected final
  URL; challenge/interstitial shapes come back flagged `captcha_or_waf`
  (degraded downstream via `classify_html`), everything else raises;
- parsed HTML is routed through the replay parser via
  `case_search.mos.parser.fetch_mos_html` — no parser fork.

This provider is deliberately NOT in the default registry under
`mos-gorsud` and NOT in any colleague-legal wiring; the production `mos-gorsud`
entry remains the QUARANTINE stub in `case_search.sources.live`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, runtime_checkable

from ..errors import (CaseSearchError, EmptyPayloadError, HttpError,
                      MalformedPayloadError, SourceUnavailableError)
from ..sources.base import DEGRADED_STATUSES, Provider, RawSourceResult
from .dom import build_tree, text_of
from .parser import detect_challenge

LIVE_SPIKE_NAME = "mos-gorsud-live-spike"

# The only public card approved for this spike (recorded in
# mos-spike/browser-evidence-02-0387-2025.json / LIVE-ADAPTER-PLAN.md).
# Read-only view: the allowlist cannot be widened by mutation at runtime.
APPROVED_CARDS: Mapping[str, str] = MappingProxyType({
    "02-0387/2025": (
        "https://m.mos-gorsud.ru/rs/hamovnicheskij/services/cases/civil/"
        "details/9ef63f11-5fa7-11ef-9d90-f3a74c864694"),
})

CARD_VISIBLE_MARKER = re.compile(r"Информация по делу\s*№")

DEFAULT_TIMEOUT_MS = 10000
MAX_TIMEOUT_MS = 15000

NavigateFn = Callable[[str, int], "BrowserPage"]


class BrowserTimeoutError(CaseSearchError):
    """Injected browser client could not navigate within its budget."""

    def __init__(self, source: str, url: str, timeout_ms: int):
        self.source = source
        self.url = url
        self.timeout_ms = timeout_ms
        super().__init__(f"navigation timeout after {timeout_ms}ms "
                         f"for '{source}'")


@dataclass(frozen=True)
class BrowserPage:
    """The single result type an injected browser client must return."""

    status: int
    final_url: str
    html: str
    challenge: bool | None = None
    retry_after: float | None = None


@runtime_checkable
class BrowserClient(Protocol):
    """Minimal browser boundary: one navigation, explicit timeout budget."""

    def navigate(self, url: str, timeout_ms: int) -> BrowserPage:
        """Navigate once to `url` and return the captured page."""


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    return rest.split("/", 1)[0].split("?", 1)[0].lower()


class MosGorsudLiveSpikeProvider(Provider):
    """Allowlisted single-navigation live adapter (browser injected).

    `lookup`/`lookup_live` never enumerate, never retry and never touch the
    filesystem. Every non-card result is raised or flagged for
    `classify_html`; only a genuine visible case card yields a plain
    `text/html` `RawSourceResult`.
    """

    name = LIVE_SPIKE_NAME
    media_type = "text/html"
    accepts_fixtures = False

    def __init__(self, browser: BrowserClient | NavigateFn, *,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.browser = browser
        self.timeout_ms = max(1, min(int(timeout_ms), MAX_TIMEOUT_MS))

    def lookup(self, case_ref: str) -> RawSourceResult:
        url = APPROVED_CARDS.get(case_ref)
        if url is None:
            raise SourceUnavailableError(
                self.name,
                f"case_ref '{case_ref}' is not on the live-spike allowlist "
                "(fail closed, no navigation attempted)")
        page = self._navigate_once(url)
        source = f"{self.name}:{case_ref}"
        retry_after = (page.retry_after
                       if page.retry_after and page.retry_after > 0
                       else None)

        if page.challenge or page.status in DEGRADED_STATUSES:
            return RawSourceResult(
                source=source, source_url=url,
                status_code=page.status, body="",
                error="captcha_or_waf", retry_after=retry_after,
                media_type="text/html")

        if not 200 <= page.status < 300:
            raise HttpError(page.status, f"live-spike '{case_ref}'")

        final_url = (page.final_url or "").rstrip("/")
        if final_url != url.rstrip("/"):
            raise MalformedPayloadError(
                source, f"unexpected final URL '{page.final_url}' (host "
                        f"'{_host_of(page.final_url or '')}' != allowed "
                        f"'{_host_of(url)}')")

        if not page.html or not page.html.strip():
            raise EmptyPayloadError(source)

        root = build_tree(page.html)
        if detect_challenge(root):
            return RawSourceResult(
                source=source, source_url=url, status_code=page.status,
                body="", error="captcha_or_waf", retry_after=retry_after,
                media_type="text/html")

        if not CARD_VISIBLE_MARKER.search(text_of(root)):
            raise MalformedPayloadError(
                source, "no visible case card marker "
                        "('Информация по делу №') — blocked/changed DOM")

        return RawSourceResult(source=source, source_url=url,
                               status_code=page.status, body=page.html,
                               media_type="text/html")

    def lookup_live(self, case_ref: str, *,
                    retrieved_at: str) -> RawSourceResult:
        """Spec interface: `retrieved_at` is caller-stamped provenance for
        the downstream parser; the raw payload itself comes from `lookup`."""
        if not retrieved_at or not retrieved_at.strip():
            raise ValueError("retrieved_at must be a non-empty timestamp")
        return self.lookup(case_ref)

    def _navigate_once(self, url: str) -> BrowserPage:
        navigate = getattr(self.browser, "navigate", None)
        if navigate is None or not callable(navigate):
            if callable(self.browser):
                navigate = self.browser
            else:
                raise SourceUnavailableError(
                    self.name, "injected browser must expose "
                               "navigate(url, timeout_ms) or be callable")
        try:
            page = navigate(url, self.timeout_ms)
        except BrowserTimeoutError:
            raise
        except TimeoutError as exc:
            raise BrowserTimeoutError(self.name, url, self.timeout_ms) \
                from exc
        if not isinstance(page, BrowserPage):
            raise MalformedPayloadError(
                self.name, "browser client must return a BrowserPage")
        return page


__all__ = ["MosGorsudLiveSpikeProvider", "BrowserPage", "BrowserClient",
           "BrowserTimeoutError", "LIVE_SPIKE_NAME", "APPROVED_CARDS",
           "CARD_VISIBLE_MARKER", "DEFAULT_TIMEOUT_MS", "MAX_TIMEOUT_MS"]
