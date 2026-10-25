"""Native embedded Camoufox browser (no external HTTP service).

Replaces the external camofox-browser HTTP service (127.0.0.1:9377) with an
in-process browser launched via the ``camoufox`` Python library. Fail-closed:
every failure surfaces as ``SudactRuntimeError``/``FetchError`` with an
actionable message — never a fabricated result.

Design:
- Lazy singleton: the browser process starts on the first ``fetch()`` call and
  stays alive for the process lifetime (per CLI invocation, per API process).
- Bounded crash recovery: if the browser dies mid-flight ("window is null",
  protocol errors), the next ``fetch()`` relaunches it once.
- GTK workaround: system ``libgtk-3.so.0`` may be absent; the bundled libs in
  ``~/.local/camofox-libs`` (same ones the external service uses) are injected
  via LD_LIBRARY_PATH before Playwright launches. Configurable through
  ``CSE_CAMOFOX_LD_PATH``.
- Thread-safety: guarded by a lock; the sync Playwright API is used, one page
  at a time.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .errors import SudactRuntimeError

NativeFetcher = Callable[[str], str]

_LAUNCH_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_BROWSER_HOLDER: dict[str, Any] = {"browser": None, "pw": None}
_RELAUNCH_TS = 0.0
_RELAUNCH_MIN_INTERVAL = 5.0

# Locations of bundled GTK libs (same set the external service uses),
# checked in order. Only the first existing path is injected.
_CANDIDATE_LD_PATHS = (
    "/home/user/.local/camofox-libs/usr/lib/x86_64-linux-gnu",
    "/home/user/.local/camofox-libs/usr/lib/x86_64-linux-gnu:"
    "/home/user/.local/camofox-libs/lib/x86_64-linux-gnu",
)


def _library_path() -> str | None:
    """Resolve LD_LIBRARY_PATH addition for the embedded browser."""
    override = os.environ.get("CSE_CAMOFOX_LD_PATH")
    if override:
        return override
    for candidate in _CANDIDATE_LD_PATHS:
        if all(os.path.exists(p) for p in candidate.split(":")):
            return candidate
    return None


def _install_cache_env() -> None:
    """Isolate the python-camoufox browser cache from the node service cache.

    The external camofox-browser service (node) and the python camoufox
    package share ~/.cache/camoufox by default but expect DIFFERENT layouts;
    a python ``camoufox fetch`` wipes the node-side metadata. Point python
    at its own XDG cache dir (its INSTALL_DIR = $XDG_CACHE_HOME/camoufox).
    """
    cache = os.environ.get("CSE_CAMOFOX_CACHE",
                           str(Path.home() / ".cache" / "camoufox-py"))
    os.environ.setdefault("XDG_CACHE_HOME", cache)


_install_cache_env()


def ensure_launch_env() -> None:
    """Inject bundled GTK libs into LD_LIBRARY_PATH.

    Must run BEFORE Playwright spawns the browser process: the driver
    propagates the current process environment at spawn time, so setting
    it later has no effect. Called at module import (idempotent).
    """
    ld = _library_path()
    if not ld:
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    if ld in current:
        return
    os.environ["LD_LIBRARY_PATH"] = f"{ld}:{current}" if current else ld


ensure_launch_env()


def native_available() -> bool:
    """True when the camoufox python package can be imported."""
    try:
        import camoufox  # noqa: F401
        return True
    except Exception:
        return False


def _launch_browser() -> Any:
    """Launch the embedded Camoufox browser; return (playwright, browser)."""
    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:  # pragma: no cover - import-time
        raise SudactRuntimeError(
            f"embedded camoufox not importable: {exc}. "
            "Install it with: pip install 'camoufox[geoip]' && "
            "python3 -m camoufox fetch"
        ) from exc

    try:
        profile_dir = Path.home() / ".cache" / "case-search" / "native-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        # Options mirror the proven node-service fingerprint defaults:
        # humanize + persistent context (fingerprint sticks across runs).
        pw_browser = Camoufox(
            headless=True,
            humanize=True,
            os="linux",
            persistent_context=True,
            user_data_dir=str(profile_dir),
            enable_cache=True,
        )
        browser = pw_browser.__enter__()
        return pw_browser, browser
    except Exception as exc:
        raise SudactRuntimeError(
            f"embedded camoufox launch failed: {exc}. "
            "Check camoufox installation and GTK libs "
            "(CSE_CAMOFOX_LD_PATH may point at bundled libs)."
        ) from exc


def _get_browser() -> Any:
    """Return the live browser, (re)launching if needed."""
    global _RELAUNCH_TS
    with _LAUNCH_LOCK:
        browser = _BROWSER_HOLDER.get("browser")
        if browser is not None:
            try:
                # Cheap liveness probe: an attached context object.
                _ = browser.browser_type  # attribute touch
                return browser
            except Exception:
                # Browser died; fall through to relaunch.
                pass
        now = time.monotonic()
        if now - _RELAUNCH_TS < _RELAUNCH_MIN_INTERVAL and \
                _BROWSER_HOLDER.get("browser") is None:
            raise SudactRuntimeError(
                "embedded camoufox relaunch cooldown active (recent crash). "
                "Retry in a few seconds."
            )
        _RELAUNCH_TS = now
        pw, browser = _launch_browser()
        _BROWSER_HOLDER["pw"] = pw
        _BROWSER_HOLDER["browser"] = browser
        return browser


def _close_browser() -> None:
    with _LAUNCH_LOCK:
        browser = _BROWSER_HOLDER.get("browser")
        pw = _BROWSER_HOLDER.get("pw")
        try:
            if pw is not None:
                pw.__exit__(None, None, None)
        except Exception:
            pass
        _BROWSER_HOLDER["browser"] = None
        _BROWSER_HOLDER["pw"] = None


def native_fetch(url: str, *, timeout: float = 60.0,
                 settle_ms: int = 1500) -> str:
    """Render ``url`` through the embedded Camoufox and return outerHTML.

    Fail-closed: any launch/navigation/evaluate failure raises
    ``SudactRuntimeError`` with an actionable message.
    """
    with _FETCH_LOCK:
        browser = _get_browser()
        page = None
        try:
            try:
                page = browser.new_page()
            except Exception as exc:
                # Browser process died between calls: one relaunch attempt.
                _close_browser()
                browser = _get_browser()
                page = browser.new_page()
            page.goto(url, timeout=int(timeout * 1000))
            # Brief settle for JS-rendered result lists.
            time.sleep(settle_ms / 1000.0)
            html = page.evaluate(
                "document.documentElement.outerHTML")
            if not isinstance(html, str) or not html.strip():
                raise SudactRuntimeError(
                    f"embedded camoufox: empty DOM for {url}")
            return html
        except SudactRuntimeError:
            raise
        except Exception as exc:
            # Mark the browser dead so the next call relaunches cleanly.
            _close_browser()
            raise SudactRuntimeError(
                f"embedded camoufox fetch failed for {url}: {exc}. "
                "The browser will relaunch on the next call."
            ) from exc
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass


def native_status() -> dict[str, Any]:
    """Machine-readable status of the embedded browser (for doctor)."""
    browser = _BROWSER_HOLDER.get("browser")
    return {
        "mode": "native",
        "available": native_available(),
        "running": browser is not None,
    }
