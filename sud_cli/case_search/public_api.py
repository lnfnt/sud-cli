"""Public Python API for agents (no subproc, no CLI).

Import surface:

    from case_search import (
        lookup_case,          # live KAD lookup via injected browser
        import_document,      # saved file -> auto-detected parser
        search_practice,      # sudact query -> result dicts
        ApiOutcome,           # envelope: status/reason/snapshot/extensions
    )

Design notes:
- Every function returns an `ApiOutcome` dataclass; exceptions from the
  fail-closed core (CaseSearchError) are caught and converted into
  `outcome.status = "error"|"degraded"` with `reason` — an agent can
  branch on `outcome.ok` without try/except plumbing, or inspect the
  underlying error via `outcome.reason`.
- `snapshot` is the canonical CaseSnapshot dataclass (typed); extensions
  (instances, events_raw) ride alongside for KAD sources.
- Network rules are unchanged: import_document/search_practice perform
  the same HTTP the CLI does; lookup_case requires an injected browser
  client (KAD live), everything else is offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import CaseSearchError, ValidationError
from .schema import CaseSnapshot
from uuid import uuid4 as _uuid4
from .storage import SnapshotStore

__version__ = "1.2.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ApiOutcome:
    """Agent-facing envelope (mirrors the CLI JSON contract)."""

    status: str                      # "ok" | "degraded" | "error"
    reason: str
    snapshot: CaseSnapshot | None = None
    extensions: dict[str, Any] = field(default_factory=dict)
    source: str = ""                 # detected source ("kad", "sudact", ...)
    stored_path: str = ""            # snapshot store path, if persisted
    changes: tuple = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "changes": [c.to_dict() for c in self.changes],
        }
        if self.extensions:
            d["extensions"] = self.extensions
        if self.stored_path:
            d["stored_path"] = self.stored_path
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)


def _err(exc: Exception, source: str = "") -> ApiOutcome:
    if isinstance(exc, CaseSearchError):
        text = str(exc)
    else:
        text = f"{type(exc).__name__}: {exc}"
    return ApiOutcome(status="error", reason=text, source=source)


def _detected_source(path: Path) -> str:
    from .cli import _detect_source
    return _detect_source(path)


def import_document(
    file_path: str | Path,
    case_ref: str,
    *,
    store_dir: str | Path | None = None,
    alerts_dir: str | Path | None = None,
    retrieved_at: str | None = None,
) -> ApiOutcome:
    """Import a saved document with auto-detected source parser.

    Supported file types and sources:
    - ``.pdf`` → VS RF parser (Supreme Court PDFs)
    - ``.json`` → KAD import (EXTRACT_EXPRESSION JSON)
    - ``.html`` with KAD markers → KAD saved-HTML import
    - ``.html`` with SudAct markers → SudAct document import
    - ``.html`` with mos-gorsud markers → Mos-Gorsud import

    Args:
        file_path: Path to the saved document file.
        case_ref: Expected case number (e.g. ``"А40-308035/2025"``).
            Used for identity verification — the parser verifies the
            document actually references this case number.
        store_dir: Optional directory for snapshot persistence.
            When given, the snapshot is stored and changes from the
            previous baseline are returned in ``changes``.
        alerts_dir: Optional directory for alert artifacts (unused
            in the current public API flow, reserved for CLI compat).
        retrieved_at: Optional ISO-8601 UTC timestamp. Defaults to
            current time.

    Returns:
        ApiOutcome with ``status="ok"`` and ``snapshot`` (CaseSnapshot)
        on success. ``extensions`` may contain ``"instances"`` and
        ``"events_raw"`` for KAD sources. On failure, ``status`` is
        ``"error"`` with a diagnostic ``reason``.
    """
    path = Path(file_path)
    try:
        source = _detected_source(path)
    except ValueError as exc:
        return _err(exc)
    except OSError as exc:
        return _err(exc)

    retrieved = retrieved_at or _utc_now()
    try:
        if source == "kad":
            from .kad.import_json import KadImportProvider
            from .kad.parser import fetch_kad_html
            provider = KadImportProvider(path, case_ref)
            outcome = fetch_kad_html(provider, case_ref, retrieved)
        elif source == "kad-html":
            from .kad.html_import import (KadHtmlImportProvider,
                                          fetch_kad_saved_html)
            provider = KadHtmlImportProvider(path, case_ref)
            outcome = fetch_kad_saved_html(provider, case_ref, retrieved)
        elif source == "sudact":
            from .sudact.parser import (SudactImportProvider,
                                        fetch_sudact_html)
            provider = SudactImportProvider(path, case_ref)
            outcome = fetch_sudact_html(provider, case_ref, retrieved)
        elif source == "vsrf":
            from .vsrf.parser import VsrfImportProvider, fetch_vsrf_pdf
            provider = VsrfImportProvider(path, case_ref)
            outcome = fetch_vsrf_pdf(provider, case_ref, retrieved)
        else:
            from .mos.parser import fetch_mos_html
            from .sources.import_html import MosDomImportProvider
            provider = MosDomImportProvider(path, case_ref)
            outcome = fetch_mos_html(provider, case_ref, retrieved)
    except CaseSearchError as exc:
        return _err(exc, source)
    except (OSError, ValueError) as exc:
        return _err(exc, source)

    extensions: dict[str, Any] = {}
    for attr in ("last_instances", "last_events"):
        val = getattr(provider, attr, None)
        if val:
            key = "instances" if attr.endswith("instances") else "events_raw"
            extensions[key] = val

    stored_path = ""
    changes: tuple = ()
    if outcome.snapshot and store_dir:
        from .snapshots import watch_case
        result = watch_case(SnapshotStore(store_dir), outcome)
        if result.stored:
            stored_path = str(result.stored.path)
        changes = tuple(result.changes)

    return ApiOutcome(
        status=outcome.status,
        reason=outcome.reason,
        snapshot=outcome.snapshot,
        extensions=extensions,
        source=source,
        stored_path=stored_path,
        changes=changes,
    )


def lookup_case(
    case_ref: str,
    *,
    browser: Callable[[str, int], Any],
    card_url: str,
    store_dir: str | Path | None = None,
    timeout_ms: int = 10000,
    retrieved_at: str | None = None,
) -> ApiOutcome:
    """Live KAD (kad.arbitr.ru) case lookup via injected browser client.

    The agent supplies its Camofox/CDP browser layer; this function
    performs no network I/O itself — it delegates to the injected
    ``browser`` callable and parses the result.

    Args:
        case_ref: Expected case number (e.g. ``"А40-308035/2025"``).
            Identity is verified against the card's reported number.
        browser: ``Callable[[str, int], Any]`` — receives
            ``(card_url, timeout_ms)`` and must return either:
            (a) an object with ``.kad_data`` dict (EXTRACT_EXPRESSION
            output), or (b) a JSON string of the extracted data.
        card_url: Full KAD card URL to navigate to.
        store_dir: Optional snapshot store directory for persistence.
        timeout_ms: Browser navigation timeout in milliseconds
            (default 10000, clamped by the browser client).
        retrieved_at: Optional ISO-8601 UTC timestamp.

    Returns:
        ApiOutcome with ``snapshot`` (CaseSnapshot) and
        ``extensions`` containing ``"instances"`` and ``"events_raw"``
        lists on success. WAF/captcha → ``status="degraded"``.
    """
    retrieved = retrieved_at or _utc_now()
    try:
        page = browser(card_url, timeout_ms)
        from .kad.parser import parse_kad_data
        # The live path is browser-runtime specific; the agent injects
        # an already-extracted data dict (EXTRACT_EXPRESSION output).
        data = getattr(page, "kad_data", None)
        if data is None and isinstance(page, str):
            try:
                data = json.loads(page)
            except (json.JSONDecodeError, ValueError):
                data = None
        if not isinstance(data, dict):
            return ApiOutcome(
                status="error",
                reason="browser client must provide EXTRACT_EXPRESSION "
                       "JSON (str) or an object with .kad_data dict",
                source="kad")
        from .schema import compute_raw_hash
        record = parse_kad_data(
            data, source="kad-live", source_url=card_url,
            retrieved_at=retrieved,
            raw_artifact_hash=compute_raw_hash(
                json.dumps(data, ensure_ascii=False, sort_keys=True)))
        if not _refs_match(record.case_number, case_ref):
            from .errors import MalformedPayloadError
            raise MalformedPayloadError(
                "kad-live", f"case identity mismatch: requested "
                            f"'{case_ref}', card reports "
                            f"'{record.case_number}'")
        snapshot = record.to_case_snapshot()
        extensions = {"instances": list(data.get("instances", [])),
                      "events_raw": list(data.get("events", []))}
        stored_path, changes = _persist(snapshot, store_dir)
        return ApiOutcome(
            status="ok", reason="normalized", snapshot=snapshot,
            extensions=extensions, source="kad",
            stored_path=stored_path, changes=changes)
    except CaseSearchError as exc:
        status = "degraded" if type(exc).__name__ == "WafDetectedError" \
            else "error"
        return ApiOutcome(status=status, reason=str(exc), source="kad")
    except Exception as exc:  # browser runtime errors
        return _err(exc, "kad")


def _refs_match(a: str, b: str) -> bool:
    from .normalize_refs import refs_match
    return refs_match(a, b)


def _persist(snapshot: CaseSnapshot,
             store_dir: str | Path | None) -> tuple[str, tuple]:
    if not store_dir:
        return "", ()
    from .pipeline import OK, FetchOutcome
    from .snapshots import watch_case
    outcome = FetchOutcome(OK, snapshot, "normalized")
    result = watch_case(SnapshotStore(store_dir), outcome)
    return (str(result.stored.path) if result.stored else "",
            tuple(result.changes))


def search_practice(
    query: str,
    *,
    section: str = "arbitral",
    page: int = 1,
    limit: int = 10,
    fetcher: Callable[[str], str] | None = None,
) -> ApiOutcome:
    """Search SudAct judicial practice by natural-language query.

    Args:
        query: Search phrase (Russian). Minimum 2 meaningful words
            recommended; single-word and stop-word queries are accepted
            but may trigger the relevance guard.
        section: SudAct section — ``"arbitral"`` (default), ``"regular"``,
            ``"magistrate"``, or ``"vsrf"``.
        page: Result page number (1-indexed). Use ``extensions["next_page"]``
            from a previous call to paginate.
        fetcher: Optional ``Callable[[str], str]`` — performs one HTTP GET
            and returns response body HTML. When ``None``, uses Camofox
            (127.0.0.1:9377) which is REQUIRED for real search results
            (sudact serves a JS-required stub to plain HTTP).

    Returns:
        ApiOutcome with ``extensions["results"]`` (list of result dicts,
        each with url/title/court/dispute/date),
        ``extensions["next_page"]`` (int or None for last page),
        ``extensions["query"]`` and ``extensions["section"]``.

    Raises:
        Never raises — all errors are returned as ``status="error"`` or
        ``status="degraded"`` outcomes.
    """
    try:
        from .sudact.search import (search as _search,
                                    normalize_section)
        section = normalize_section(section)
        if fetcher is None:
            from .sudact.runtime import js_fetcher
            fetcher = js_fetcher(timeout=90.0)
        result = _search(query, fetcher, section=section, page=page,
                         limit=limit)
        d = result.to_dict()
        return ApiOutcome(
            status="ok", reason="search-ok", snapshot=None,
            extensions={"results": d["results"],
                        "next_page": d["next_page"],
                        "query": d["query"], "section": d["section"]},
            source=f"sudact-search:{section}")
    except CaseSearchError as exc:
        status = "degraded" if type(exc).__name__ == "WafDetectedError" \
            else "error"
        return ApiOutcome(status=status, reason=str(exc),
                          source=f"sudact-search:{section}")
    except Exception as exc:
        return _err(exc, f"sudact-search:{section}")


def search_iter(
    query: str,
    *,
    section: str = "arbitral",
    fetcher: Callable[[str], str] | None = None,
    max_pages: int = 10,
) -> "Iterator[ApiOutcome]":
    """Paginate through all search result pages for a query.

    Yields one ApiOutcome per page. Stops when ``next_page`` is None,
    when a non-ok outcome is encountered, or when *max_pages* is reached.

    Usage::

        for page_outcome in search_iter("банкротство физлиц"):
            if not page_outcome.ok:
                break
            for result in page_outcome.extensions["results"]:
                print(result["title"], result["url"])

    Args:
        query: Search phrase (Russian).
        section: SudAct section (default ``"arbitral"``).
        fetcher: Optional fetcher callable (see :func:`search_practice`).
        max_pages: Safety cap to prevent unbounded iteration (default 10).

    Yields:
        ApiOutcome per page with same extensions as search_practice.
    """
    page = 1
    for _ in range(max_pages):
        outcome = search_practice(query, section=section, page=page,
                                 fetcher=fetcher)
        yield outcome
        if not outcome.ok:
            return
        next_page = outcome.extensions.get("next_page")
        if next_page is None:
            return
        page = next_page


def get_practice_doc(
    doc_url: str,
    case_ref: str | None = None,
    *,
    store_dir: str | Path | None = None,
    retrieved_at: str | None = None,
    browser_fetch: Callable[[str], str] | None = None,
) -> ApiOutcome:
    """Fetch and parse a SudAct document page by URL.

    SudAct document pages (``/doc/<id>/``) are server-rendered HTML —
    plain stdlib urllib works (no JS required). Pass *browser_fetch*
    only for pages that need JS rendering.

    Args:
        doc_url: Full URL of the sudact.ru document page.
        case_ref: Expected case number for identity verification.
            When ``None``, the case number is auto-extracted from the
            document text (first match of the case-number pattern).
            Enables the one-call flow: ``search_practice`` →
            ``get_practice_doc(result["url"])`` → full snapshot.
        store_dir: Optional snapshot store directory for persistence.
        retrieved_at: Optional ISO timestamp (defaults to current UTC).
        browser_fetch: Optional ``Callable[[str], str]`` for JS pages.

    Returns:
        ApiOutcome with ``snapshot`` (CaseSnapshot) on success.
    """
    retrieved = retrieved_at or _utc_now()
    try:
        if browser_fetch is not None:
            html = browser_fetch(doc_url)
        else:
            import urllib.request as _ureq
            from sud_cli.sud_netguard import check_before_fetch
            check_before_fetch(doc_url)  # netguard: fail-closed egress+breaker
            req = _ureq.Request(doc_url,
                                headers={"User-Agent": "Mozilla/5.0"})
            with _ureq.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")

        if case_ref is None:
            import re as _re
            nums = _re.findall(r"[А-ЯA-Z0-9]{1,6}-\d{2,7}/\d{2,4}", html)
            if not nums:
                return ApiOutcome(
                    status="error",
                    reason="case_ref not given and no case number found "
                           "in document",
                    source=f"sudact-doc:{doc_url}")
            case_ref = nums[0]

        from .sudact.parser import (SudactImportProvider,
                                    fetch_sudact_html, extract_full_text)
        full_text = extract_full_text(html)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            doc_file = Path(tmp) / "doc.html"
            doc_file.write_text(html, encoding="utf-8")
            provider = SudactImportProvider(doc_file, case_ref)
            outcome = fetch_sudact_html(provider, case_ref, retrieved)

        stored_path, changes = _persist(outcome.snapshot, store_dir)

        # Full act text: saved next to the snapshot when store_dir given,
        # otherwise returned in extensions (agents need the act body).
        text_path = ""
        extensions: dict[str, Any] = {}
        if full_text:
            extensions["full_text_chars"] = len(full_text)
            if store_dir:
                text_path = str(Path(store_dir) /
                                f"{case_ref.replace('/', '-')}.txt")
                Path(text_path).parent.mkdir(parents=True, exist_ok=True)
                Path(text_path).write_text(full_text, encoding="utf-8")
            else:
                extensions["full_text"] = full_text

        return ApiOutcome(
            status=outcome.status, reason=outcome.reason,
            snapshot=outcome.snapshot, source=f"sudact-doc:{doc_url}",
            extensions=extensions, stored_path=stored_path,
            changes=changes)
    except CaseSearchError as exc:
        status = "degraded" if type(exc).__name__ == "WafDetectedError" \
            else "error"
        return ApiOutcome(status=status, reason=str(exc),
                          source=f"sudact-doc:{doc_url}")
    except Exception as exc:
        return _err(exc, f"sudact-doc:{doc_url}")


def case_history(
    case_number: str,
    *,
    section: str = "arbitral",
    max_related: int = 5,
    limit: int = 10,
    fetcher: Callable[[str], str] | None = None,
) -> ApiOutcome:
    """Судьба дела одним вызовом: все акты по номеру дела + связанным
    инстанциям (апелляция/кассация), найденным в sudact.

    Returns ApiOutcome with `extensions` = case history dict:
    {"instances": {номер: {"documents": [...]}},
     "related_numbers": [...], "total_documents": N}
    """
    try:
        from .sudact.case_history import search_case_history as _sch

        if fetcher is None:
            from .sudact.runtime import js_fetcher
            fetcher = js_fetcher(timeout=90.0)

        result = _sch(case_number, fetcher, section=section,
                      max_related=max_related, limit=limit)
        if result.get("total_documents", 0) == 0:
            return ApiOutcome(
                status="error", reason=result.get("error",
                       "case history is empty (acts not published yet?)"),
                source=f"sudact-case-history:{section}",
                extensions=result)
        return ApiOutcome(
            status="ok", reason="case-history-ok", snapshot=None,
            extensions={k: v for k, v in result.items()
                        if k != "parser_version"},
            source=f"sudact-case-history:{section}")
    except CaseSearchError as exc:
        status = "degraded" if type(exc).__name__ == "WafDetectedError" \
            else "error"
        return ApiOutcome(status=status, reason=str(exc),
                          source=f"sudact-case-history:{section}")
    except Exception as exc:
        return _err(exc, f"sudact-case-history:{section}")


def _camofox_browser_ops(base: str, user: str):
    """Build a KadBrowserOps-compatible client for the Camofox runtime."""
    import json as _json
    import time as _time
    import urllib.request as _ureq

    def _api(endpoint: str, payload: dict | None = None) -> dict:
        req = _ureq.Request(
            f"{base}{endpoint}",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST")
        with _ureq.urlopen(req, timeout=120) as resp:
            return _json.load(resp)

    class _Ops:
        def __init__(self):
            tab = _api("/tabs", {"userId": user, "sessionKey": user})
            self.tab_id = tab["tabId"]
        def navigate(self, url):
            _api(f"/tabs/{self.tab_id}/navigate",
                 {"userId": user, "sessionKey": user, "url": url})
        def evaluate(self, js):
            r = _api(f"/tabs/{self.tab_id}/evaluate",
                     {"userId": user, "sessionKey": user,
                      "expression": js})
            return str(r["result"])
        def wait_seconds(self, s):
            _time.sleep(s)

    return _Ops()


def _native_browser_ops():
    """KadBrowserOps-compatible ops over the embedded Camoufox (no service).

    Singleton page per ops-object; crash-recovery: one relaunch attempt on
    navigation/evaluate failure (mirrors browser_native.fetch semantics).
    """
    import json as _json
    import time as _time
    from .browser_native import _get_browser, _close_browser

    class _NativeOps:
        def __init__(self):
            self.page = None
        def _ensure_page(self):
            if self.page is None:
                browser = _get_browser()
                self.page = browser.new_page()
            return self.page
        def navigate(self, url):
            try:
                page = self._ensure_page()
                page.goto(url, timeout=60000, wait_until="load")
            except Exception as exc:
                # Browser died: one relaunch attempt.
                self.page = None
                _close_browser()
                page = self._ensure_page()
                page.goto(url, timeout=60000, wait_until="load")
        def evaluate(self, js):
            page = self._ensure_page()
            r = page.evaluate(js)
            return r if isinstance(r, str) else _json.dumps(r, ensure_ascii=False)
        def wait_seconds(self, s):
            _time.sleep(s)

    return _NativeOps()


def kad_case_history(
    case_number: str,
    *,
    camofox_base: str | None = None,  # default from config
    camofox_user: str = "",
    chrono_wait: float = 4.0,
    card_wait: float = 6.0,
) -> ApiOutcome:
    """Судьба дела по номеру через КАД (Arbitr) одним вызовом.

    Требует Camofox (127.0.0.1:9377). Flow: UI-поиск КАД (без капчи)
    → UUID → карточка → все инстанции + хронология + события.
    extensions: {search, kad_uuid, kad_url, card, instances, events}
    """
    try:
        from .kad.search import KadSearchError, kad_case_history as _kch

        if not camofox_base or camofox_base == "native":
            ops = _native_browser_ops()
        else:
            user = camofox_user or f"cs-kad-{_uuid4().hex[:8]}"
            ops = _camofox_browser_ops(camofox_base, user)
        result = _kch(case_number, ops, chrono_wait=chrono_wait,
                      card_wait=card_wait)
        return ApiOutcome(
            status="ok", reason="kad-case-history-ok", snapshot=None,
            extensions={k: v for k, v in result.items()
                        if k != "parser_version"},
            source="kad-case-history")
    except KadSearchError as exc:
        return ApiOutcome(status="error", reason=str(exc),
                          source="kad-case-history")
    except Exception as exc:
        return _err(exc, "kad-case-history")


def kad_search(
    inn: str | None = None,
    ogrn: str | None = None,
    name: str | None = None,
    *,
    page: int = 1,
    camofox_base: str | None = None,
    camofox_user: str = "",
    browser_ops=None,
) -> ApiOutcome:
    """Search KAD (kad.arbitr.ru) cases by participant.

    Requires Camofox (127.0.0.1:9377) unless a ``browser_ops`` object is
    injected. Returns an ApiOutcome with ``extensions`` containing the
    search result dict (``hits``, ``provenance``, ``page``).

    Captcha / WAF / unexpected response -> ``status="degraded"`` with a
    machine-readable reason (PRACTICE_CAPTCHA / WAF_BLOCKED / ...).
    Empty input -> ``status="error"`` (ValidationError).
    """
    try:
        from .kad.search import kad_search as _ks

        if browser_ops is None:
            if not camofox_base or camofox_base == "native":
                browser_ops = _native_browser_ops()
            else:
                user = camofox_user or f"cs-kad-{_uuid4().hex[:8]}"
                browser_ops = _camofox_browser_ops(camofox_base, user)
        result = _ks(inn=inn, ogrn=ogrn, name=name, page=page,
                     ops=browser_ops, retrieved_at=_utc_now())
        return ApiOutcome(
            status=result.status,
            reason=result.reason,
            snapshot=None,
            extensions=result.to_dict(),
            source="kad-search")
    except ValidationError as exc:
        return ApiOutcome(status="error", reason=str(exc),
                          source="kad-search")
    except Exception as exc:
        return _err(exc, "kad-search")


def get_decision_text(url_or_id: str, *, store_dir: str | None = None,
                      use_cache: bool = True) -> ApiOutcome:
    """Extract the clean full text of a court decision (Full Text Extractor).

    Accepts a sudact.ru document URL (regular/arbitral/magistrate/vsrf)
    or a local file path (saved HTML / PDF). Returns clean act text in
    ``extensions["full_text"]`` with structural blocks in
    ``extensions["blocks"]`` (intro / descriptive / motivation /
    resolution keyed by УСТАНОВИЛ / РЕШИЛ / ОПРЕДЕЛИЛ markers).

    Caching: texts are cached in a local SQLite DB keyed by URL hash;
    repeated calls do not touch the network (set ``use_cache=False``
    to force a refetch).
    """
    try:
        import hashlib as _hashlib
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path

        text = ""
        source_kind = ""

        # 1. Local file path (HTML or PDF)
        p = _Path(url_or_id)
        if p.is_file():
            source_kind = f"file:{p.suffix.lower()}"
            if p.suffix.lower() == ".pdf":
                from .vsrf.parser import extract_pdf_text
                text = extract_pdf_text(p)
            else:
                raw = p.read_bytes()
                try:
                    html = raw.decode("utf-8")
                except UnicodeDecodeError:
                    html = raw.decode("cp1251", errors="replace")
                if "sudact.ru" in html[:4000] or "sudact" in html:
                    from .sudact.parser import extract_full_text as _eft
                    text = _eft(html)
                else:
                    from .sudrf.parser import extract_full_text as _geft
                    text = _geft(html)
        else:
            # 2. Remote sudact URL via Camofox
            if "sudact.ru/" not in url_or_id:
                return ApiOutcome(
                    status="error",
                    reason="UNSUPPORTED_SOURCE: pass a sudact.ru document "
                           "URL or a local file path",
                    source="decision-text")
            url = url_or_id.split("?")[0]

            # 2a. SQLite cache lookup
            cache_path = (_Path(store_dir) if store_dir
                          else _Path.home() / ".cache" / "case-search")
            cache_path.mkdir(parents=True, exist_ok=True)
            db = cache_path / "decisions_cache.sqlite"
            url_hash = _hashlib.sha256(url.encode()).hexdigest()
            if use_cache:
                try:
                    con = _sqlite3.connect(str(db))
                    row = con.execute(
                        "SELECT text FROM decisions WHERE url_hash=?",
                        (url_hash,)).fetchone()
                    con.close()
                    if row:
                        text = row[0]
                        source_kind = "cache"
                except _sqlite3.Error:
                    pass

            # 2b. Fetch live if no cache hit
            if not text:
                from .browser_native import native_fetch
                html = native_fetch(url, timeout=90.0, settle_ms=4000)

                from .sudact.parser import extract_full_text as _eft
                text = _eft(html)
                source_kind = "sudact-live"

                # 2c. Cache write
                if text:
                    try:
                        con = _sqlite3.connect(str(db))
                        con.execute(
                            "CREATE TABLE IF NOT EXISTS decisions "
                            "(url_hash TEXT PRIMARY KEY, url TEXT, "
                            "text TEXT, cached_at TEXT)")
                        con.execute(
                            "INSERT OR REPLACE INTO decisions VALUES "
                            "(?, ?, ?, datetime('now'))",
                            (url_hash, url, text))
                        con.commit()
                        con.close()
                    except _sqlite3.Error:
                        pass

        if not text or not text.strip():
            return ApiOutcome(
                status="error",
                reason="EMPTY_TEXT: no act text extracted (scan? use OCR)",
                source="decision-text")

        # 3. Structural blocks: УСТАНОВИЛ / РЕШИЛ / ОПРЕДЕЛИЛ
        import re as _re
        blocks: dict[str, str] = {}
        markers = [("motivation", r"[Уу]станови(?:л|ла)\s*:?"),
                   ("resolution", r"(?:[Рр]ешил|[Оо]предели)(?:л|ла)?\s*:?")]
        positions = []
        for name, pat in markers:
            m = _re.search(pat, text)
            if m:
                positions.append((m.start(), name))
        positions.sort()
        for idx, (pos, name) in enumerate(positions):
            end = positions[idx + 1][0] if idx + 1 < len(positions) else len(text)
            blocks[name] = text[pos:end].strip()
        intro = text[:positions[0][0]].strip() if positions else text

        extensions: dict[str, object] = {
            "full_text": text,
            "full_text_chars": len(text),
            "blocks": blocks,
            "intro": intro[:2000],
            "source_kind": source_kind,
        }
        if source_kind == "cache":
            extensions["cache_hit"] = True

        # Optional: persist text next to snapshot store
        text_path = ""
        if store_dir:
            safe = _re.sub(r"[^\w-]", "_",
                           url_or_id.split("/")[-1] or "decision")[:80]
            text_path = str(_Path(store_dir) / f"{safe}.txt")
            _Path(text_path).write_text(text, encoding="utf-8")

        return ApiOutcome(
            status="ok", reason="decision-text-ok", snapshot=None,
            extensions=extensions, source=f"decision-text:{url_or_id}")
    except Exception as exc:
        if isinstance(exc, ConnectionRefusedError) or \
                "Connection refused" in str(exc):
            code = "CAMOFOX_UNAVAILABLE"
        else:
            code = "EXTRACT_ERROR"
        return ApiOutcome(
            status="error",
            reason=f"{code}: {type(exc).__name__}: {exc}"[:300],
            source="decision-text")


def get_sudrf_case(case_number: str, *, host: str,
                   date_window: int = 5,
                   proxy: str | None = None) -> ApiOutcome:
    """Live ГАС «Правосудие» case card by number (no browser needed).

    Resolves the number via hearings-listing scan on ``host``
    (GAS has no GET search-by-number), then fetches the full card:
    УИД, header (категория/судья/результат), ДВИЖЕНИЕ ДЕЛА,
    УЧАСТНИКИ. Works over plain HTTP through the Finnish proxy.

    On success returns a canonical ``CaseSnapshot`` in ``snapshot``;
    the raw SudRF result dict is preserved in ``extensions``.
    """
    try:
        if proxy is None:
            from .config import get_proxy
            proxy = get_proxy()
        from .sudrf.live import (get_live_case, SudrfLiveError,
                                 sudrf_result_to_snapshot)
        result = get_live_case(case_number, host=host,
                               date_window=date_window, proxy=proxy)
        snapshot = sudrf_result_to_snapshot(
            result, source=f"sudrf-live:{host}")
        return ApiOutcome(
            status="ok", reason="sudrf-live-ok", snapshot=snapshot,
            extensions=result, source=f"sudrf-live:{host}")
    except SudrfLiveError as exc:
        code = "CASE_NOT_LISTED" if "not found in listings" in str(exc) \
            else "SUDRF_LIVE_ERROR"
        return ApiOutcome(
            status="error", reason=f"{code}: {exc}",
            source=f"sudrf-live:{host}")
    except Exception as exc:
        if "Connection refused" in str(exc) or "timeout" in str(exc).lower():
            code = "PROXY_UNAVAILABLE"
        else:
            code = "SUDRF_LIVE_ERROR"
        return ApiOutcome(
            status="degraded", reason=f"{code}: {exc}"[:300],
            source=f"sudrf-live:{host}")


def get_sudrf_case_by_vnkod(vnkod: str, case_number: str, *,
                            date_window: int = 5,
                            explicit_dates: list[str] | None = None,
                            proxy: str | None = None) -> ApiOutcome:
    """Live GAS case card for ANY court by its vnkod (catalog code).

    Uses registry hosts[vnkod].host — covers 2366 courts (95% of GAS
    courts have official subdomains). No browser needed.

    On success returns a canonical ``CaseSnapshot`` in ``snapshot``;
    the raw SudRF result dict is preserved in ``extensions``.
    """
    try:
        if proxy is None:
            from .config import get_proxy
            proxy = get_proxy()
        from .sudrf.live import (get_live_case_by_vnkod as _fn,
                                 SudrfLiveError, sudrf_result_to_snapshot)
        result = _fn(vnkod, case_number, date_window=date_window,
                     explicit_dates=explicit_dates, proxy=proxy)
        snapshot = sudrf_result_to_snapshot(
            result, source=f"sudrf-live:{result['host']}")
        return ApiOutcome(
            status="ok", reason="sudrf-live-ok", snapshot=snapshot,
            extensions=result, source=f"sudrf-live:{result['host']}")
    except SudrfLiveError as exc:
        code = "CASE_NOT_LISTED" if "not found in listings" in str(exc) \
            else "NO_HOST_IN_REGISTRY" if "no host in registry" in str(exc) \
            else "SUDRF_LIVE_ERROR"
        return ApiOutcome(
            status="error", reason=f"{code}: {exc}",
            source=f"sudrf-live:{vnkod}")
    except Exception as exc:
        code = "PROXY_UNAVAILABLE" if ("refused" in str(exc) or
                                       "timeout" in str(exc).lower()) \
            else "SUDRF_LIVE_ERROR"
        return ApiOutcome(
            status="degraded", reason=f"{code}: {exc}"[:300],
            source=f"sudrf-live:{vnkod}")


def get_mos_case(court_slug: str, case_number: str, *,
                 kind: str | None = None) -> ApiOutcome:
    """Live case card from mos-gorsud.ru (Moscow courts).

    ``court_slug`` — portal slug (e.g. ``horoshevskij``, see
    ``mos.live.KNOWN_COURTS``). ``case_number`` — e.g. ``02-2101/2026``.
    No browser needed (server-rendered HTML over plain HTTPS).

    On success returns a canonical ``CaseSnapshot`` in ``snapshot``; the
    raw mos card dict is preserved in ``extensions`` for backward
    compatibility.
    """
    try:
        from .mos.live import (get_mos_case as _fn, MosLiveError,
                               mos_card_to_snapshot)
        from .config import get_proxy
        result = _fn(court_slug, case_number, kind=kind,
                     proxy=get_proxy())
        warnings: list[str] = []
        meta = result.get("events_timeline_meta")
        if isinstance(meta, dict):
            warnings.extend(meta.get("warnings", []))
        snapshot = mos_card_to_snapshot(result, warnings=warnings)
        return ApiOutcome(
            status="ok", reason="mos-live-ok", snapshot=snapshot,
            extensions=result, source=f"mos-live:{court_slug}")
    except MosLiveError as exc:
        code = "CASE_NOT_FOUND" if "not found" in str(exc) \
            else "MOS_LIVE_ERROR"
        return ApiOutcome(
            status="error", reason=f"{code}: {exc}",
            source=f"mos-live:{court_slug}")
    except Exception as exc:
        return ApiOutcome(
            status="degraded",
            reason=f"MOS_LIVE_ERROR: {type(exc).__name__}: {exc}"[:300],
            source=f"mos-live:{court_slug}")


__all__ = ["ApiOutcome", "lookup_case", "import_document",
           "get_decision_text", "get_sudrf_case",
           "get_sudrf_case_by_vnkod", "get_mos_case",
           "search_practice", "search_iter", "get_practice_doc",
           "case_history", "kad_case_history", "kad_search",
           "__version__"]
