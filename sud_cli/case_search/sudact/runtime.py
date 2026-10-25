"""Browser-runtime (Camofox) probe and JS fetcher for sudact.ru.

The Camofox runtime exposes a loopback HTTP API on ``http://127.0.0.1:9377``.
This module is the only package boundary that talks to that runtime; it
provides a lightweight readiness probe and a generic JS-capable fetcher that
can be injected into :mod:`case_search.sudact.search`.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
import uuid

from ..errors import SudactRuntimeError
from .search import Fetcher, FetchError

DEFAULT_BASE_URL = "http://127.0.0.1:9377"
_HEALTH_PATH = "/health"


def probe_browser_runtime(base_url: str = DEFAULT_BASE_URL,
                          timeout: float = 5.0) -> dict:
    """Lightweight HTTP health probe of the browser runtime.

    Probes ``GET {base_url}/health`` (Camofox exposes this endpoint; it does
    not start a browser session).  The function never raises; it always
    returns a dict with the shape required by the agent contract.

    Returns:
        {"ok": bool, "endpoint": str, "status": str|None, "error": str}
    """
    endpoint = base_url.rstrip("/") + _HEALTH_PATH
    try:
        req = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = str(resp.status)
            try:
                data = json.load(resp)
            except Exception:
                data = None
            ok = resp.status < 400 and (
                data is None or data.get("ok") is not False)
            return {
                "ok": ok,
                "endpoint": endpoint,
                "status": status,
                "error": "" if ok else "runtime reported not ok",
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "status": str(exc.code),
            "error": f"HTTP {exc.code}",
        }
    except socket.timeout:
        return {
            "ok": False,
            "endpoint": endpoint,
            "status": "timeout",
            "error": "connection timed out",
        }
    except Exception as exc:  # pragma: no cover - catch-all safety
        return {
            "ok": False,
            "endpoint": endpoint,
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _api(base_url: str, user: str, endpoint: str,
         payload: dict | None = None, timeout: float = 30.0) -> dict:
    """Make one Camofox runtime API call and return the JSON body."""
    url = base_url.rstrip("/") + endpoint
    if payload is None:
        req = urllib.request.Request(
            f"{url}?userId={user}&sessionKey={user}", method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise FetchError(
            f"browser runtime HTTP {exc.code}: {body}") from exc


def _is_retriable_tab_error(exc: FetchError) -> bool:
    """True when the error matches a transient dead-browser state."""
    msg = str(exc).lower()
    if "http 500" not in msg:
        return False
    markers = ("newpage", "window is null", "browserconnected")
    return any(marker in msg for marker in markers)


def _open_tab_with_retry(base: str, user: str,
                         attempts: int = 3, backoff: float = 1.5,
                         timeout: float = 20.0) -> dict:
    """Open a Camofox tab, retrying transient browser-death errors.

    The runtime can return HTTP 500 when the browser process underneath
    has died (e.g. ``browserContext.newPage: Protocol error ... window is
    null``).  Those states are transient: a service restart (or sometimes
    just the next request after the runtime recycles) recovers them, so
    we retry a bounded number of times before giving up.
    """
    last_error: FetchError | None = None
    for attempt in range(attempts):
        try:
            return _api(base, user, "/tabs",
                        {"userId": user, "sessionKey": user},
                        timeout=timeout)
        except FetchError as exc:
            last_error = exc
            if not _is_retriable_tab_error(exc):
                raise
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
    # Exhausted retries: build an actionable error with live health facts.
    status = runtime_status(base)
    short = str(last_error).replace("\n", " ")[:200] if last_error else ""
    health_bits = []
    bc = status.get("browserConnected")
    ps = status.get("poolSize")
    if bc is not None:
        health_bits.append(f"browserConnected={bc}")
    if ps is not None:
        health_bits.append(f"poolSize={ps}")
    health_facts = f" ({'; '.join(health_bits)})" if health_bits else ""
    raise SudactRuntimeError(
        f"browser runtime is up but its browser died (HTTP 500: {short}). "
        "Restart the camofox service or point --runtime-url elsewhere."
        f"{health_facts}"
    )


def runtime_status(base_url: str | None = None,
                   timeout: float = 5.0) -> dict:
    """Probe the runtime and parse the /health payload when available.

    Returns a dict combining the probe result with parsed health fields:
    browserConnected, poolSize, engine, version.  Missing fields are None.
    """
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    probe = probe_browser_runtime(base_url=base, timeout=timeout)
    result: dict = {
        "ok": probe["ok"],
        "endpoint": probe["endpoint"],
        "status": probe["status"],
        "error": probe["error"],
        "browserConnected": None,
        "poolSize": None,
        "engine": None,
        "version": None,
    }
    endpoint = base + _HEALTH_PATH
    try:
        req = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            if isinstance(data, dict):
                result["browserConnected"] = data.get("browserConnected")
                result["poolSize"] = data.get("poolSize")
                result["engine"] = data.get("engine")
                result["version"] = data.get("version")
    except Exception:
        pass
    return result


def _close_tab(base_url: str, user: str, tab_id: str) -> None:
    """Best-effort tab cleanup; errors are ignored."""
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/tabs/{tab_id}",
            data=json.dumps({"userId": user,
                             "sessionKey": user}).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception:
        pass


def _poll_ready(base_url: str, user: str, tab_id: str,
                deadline: float) -> None:
    """Wait until the page is ready, bounded by ``deadline``."""
    expr = "JSON.stringify({ready:document.readyState,body:!!document.body})"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            r = _api(base_url, user, f"/tabs/{tab_id}/evaluate",
                     {"userId": user, "sessionKey": user,
                      "expression": expr},
                     timeout=min(5.0, remaining))
            info = json.loads(r["result"])
            if info.get("ready") == "complete" and info.get("body"):
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise FetchError("browser runtime: page did not become ready in time")


def js_fetcher(base_url: str | None = None,
               timeout: float = 60.0) -> Fetcher:
    """Return a :class:`Fetcher` that renders ``url`` through Camofox.

    Mode resolution (2026-09-19, native-first):
    - ``base_url=None`` (Python API default) → embedded in-process Camoufox
      (``case_search.browser_native``). No external service required.
    - ``base_url="native"`` → force embedded mode (CLI escape hatch).
    - explicit ``base_url="http://..."`` → external camofox-browser service
      (legacy mode, kept for compatibility).

    Raises:
        SudactRuntimeError: if the runtime health probe fails before any
        tab is created (external mode) or the embedded launch fails.
    """
    if base_url is None or base_url == "native":
        from ..browser_native import native_fetch
        return lambda url: native_fetch(url, timeout=timeout)

    probe = probe_browser_runtime(base_url, timeout=min(5.0, timeout))
    if not probe["ok"]:
        err = probe.get("error") or "unreachable"
        raise SudactRuntimeError(
            f"browser runtime not available at {base_url}: {err}. "
            "Start the camofox runtime service, drop --runtime-url to use "
            "the embedded browser, or pass --runtime-url native."
        )

    base = base_url.rstrip("/")
    user = f"cs-sudact-{uuid.uuid4().hex[:8]}"

    def fetch(url: str) -> str:
        last_exc: Exception | None = None
        # Bounded retry for transient dead-browser states (HTTP 500 with
        # "window is null" / browserConnected=false): a fresh tab creation
        # after the runtime recycles usually recovers. Mirrors the
        # _open_tab_with_retry policy at the navigate/evaluate stage.
        for attempt in range(2):
            tab = _open_tab_with_retry(base, user,
                                       timeout=min(20.0, timeout))
            tab_id = tab["tabId"]
            try:
                _api(base, user, f"/tabs/{tab_id}/navigate",
                     {"userId": user, "sessionKey": user, "url": url},
                     timeout=min(45.0, timeout))
                _poll_ready(base, user, tab_id,
                            time.monotonic() + timeout)
                # Brief settle for JS-rendered result lists.
                time.sleep(1.5)
                r = _api(base, user, f"/tabs/{tab_id}/evaluate",
                         {"userId": user, "sessionKey": user,
                          "expression": "document.documentElement.outerHTML"},
                         timeout=min(20.0, timeout))
                return r["result"]
            except FetchError as exc:
                last_exc = exc
                if attempt >= 1 or not _is_retriable_tab_error(exc):
                    raise
                # Fall through to retry with a fresh tab.
            finally:
                _close_tab(base, user, tab_id)
        raise last_exc  # unreachable; satisfies type checkers

    return fetch


__all__ = [
    "DEFAULT_BASE_URL",
    "probe_browser_runtime",
    "runtime_status",
    "js_fetcher",
]
