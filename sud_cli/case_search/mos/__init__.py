"""Bounded mos-gorsud DOM package (phase 3 spike + live adapter boundary).

Replay-only: parse saved, PII-redacted HTML fixtures with stdlib
`html.parser`. `live_spike` provides an injected-browser adapter BOUNDARY
(allowlisted single navigation, no browser runtime, no network imports); the
production `mos-gorsud` registry entry remains QUARANTINE.
"""

from .dom import build_tree, text_of
from .live_spike import (APPROVED_CARDS, DEFAULT_TIMEOUT_MS,
                         MAX_TIMEOUT_MS, BrowserClient, BrowserPage,
                         BrowserTimeoutError, MosGorsudLiveSpikeProvider)
from .parser import (MOS_DOM_PARSER_VERSION, MosAppealRef, MosDocument,
                     MosHearing, MosRecord, MosTimelineEvent, classify_html,
                     detect_challenge, fetch_mos_html, parse_mos_html)

__all__ = ["MOS_DOM_PARSER_VERSION", "MosRecord", "MosTimelineEvent",
           "MosHearing", "MosDocument", "MosAppealRef", "classify_html",
           "detect_challenge", "parse_mos_html", "fetch_mos_html",
           "build_tree", "text_of", "MosGorsudLiveSpikeProvider",
           "BrowserClient", "BrowserPage", "BrowserTimeoutError",
           "APPROVED_CARDS", "DEFAULT_TIMEOUT_MS", "MAX_TIMEOUT_MS"]
