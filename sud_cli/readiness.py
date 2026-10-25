"""Live-readiness self-check for AI agents driving sud-cli.

An agent that only reads ``--help`` cannot tell that every court command needs
``SUD_CLI_BRIDGE_LIVE=1``, that ``lookup`` needs ``--source`` or that the case
reference uses a two-digit year. Each of those was discovered by hand on
2026-10-09, at the cost of a full debugging session. This module answers the
same questions in one call, without contacting any court host.

Everything here is local: the checks only inspect configuration, the
case-search binary on PATH, and the optional Camofox runtime on loopback.
No court request is made, so the check is safe to run before every session.

``check_live_readiness`` returns a mapping with one entry per concern, each
carrying ``ok``, ``detail`` and — where it applies — ``remedy``, so an agent
can act on the output without reading the source.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import urlopen
from urllib.error import URLError

#: Environment variable that opts the bridge into live court access.
LIVE_ENV_VAR = "SUD_CLI_BRIDGE_LIVE"

#: Environment variable that routes case-search at an external Camofox
#: runtime. Required on this host: the in-process browser cannot initialise
#: inside the bridge's child ("Playwright Sync API inside the asyncio loop"),
#: so every JS-capable command fails without it.
CAMOFOX_ENV_VAR = "CSE_CAMOFOX_URL"

#: Default loopback Camofox endpoint used when the variable is unset.
DEFAULT_CAMOFOX_URL = "http://127.0.0.1:9377"

#: Values of ``SUD_CLI_BRIDGE_LIVE`` that count as enabled. Kept in step with
#: ``case_search_bridge._live_access_enabled`` — an unset variable must never
#: open network access.
LIVE_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})

#: Real court references, covering each prefix shape the bridge accepts.
#: Prefixes are mixed alphanumerics of up to 4 characters and letters are legal
#: in ANY position — ГАС Правосудие prints ``2а-2939/2025`` with a digit first.
#: These are examples for the agent, not a validation contract: the pattern
#: itself lives in ``case_search_bridge`` and is exercised by its own tests.
CASE_REFERENCE_EXAMPLES = (
    ("А40-11111/25", "АС Москвы — the abbreviated two-digit year courts print"),
    ("А40-11111/2025", "same case, four-digit year"),
    ("33-35073/2026", "SudRF, no letter prefix"),
    ("2а-2939/2025", "ГАС Правосудие — district court, digit before letter"),
    ("16АП-12345/2025", "appellate instance, letter suffix"),
)

#: Commands that reach an external court host through the bridge.
LIVE_COMMANDS = (
    "lookup",
    "case-history",
    "sudrf-live",
    "mos-live",
    "search-practice",
    "kad-search",
    "get-text",
)


def _entry(ok: bool, detail: str, remedy: str = "") -> Dict[str, Any]:
    entry: Dict[str, Any] = {"ok": ok, "detail": detail}
    if remedy:
        entry["remedy"] = remedy
    return entry


def _live_flag_enabled(environ: Dict[str, str]) -> bool:
    return str(environ.get(LIVE_ENV_VAR, "")).strip().lower() in LIVE_ENABLED_VALUES


def _check_live_flag(environ: Dict[str, str]) -> Dict[str, Any]:
    raw = environ.get(LIVE_ENV_VAR)
    if raw is None:
        return _entry(
            False,
            f"{LIVE_ENV_VAR} is not set; all court commands return 'blocked'",
            f"export {LIVE_ENV_VAR}=1",
        )
    if _live_flag_enabled(environ):
        return _entry(True, f"{LIVE_ENV_VAR}={raw} enables live court access")
    return _entry(
        False,
        f"{LIVE_ENV_VAR}={raw!r} is not an enabling value",
        f"export {LIVE_ENV_VAR}=1",
    )


def _check_binary(environ: Dict[str, str]) -> Dict[str, Any]:
    path = environ.get("PATH", "")
    # An explicitly empty PATH must read as "not installed", not as "fall back
    # to os.defpath": shutil.which(cmd, path=None) searches the default PATH
    # and would report a binary the caller cannot actually reach.
    found = shutil.which("case-search", path=path) if path else None
    if not found:
        return _entry(
            False,
            "case-search is not on PATH",
            "install case-search v1.2.0 or add it to PATH",
        )
    return _entry(True, f"case-search found at {found}")


def _check_license(environ: Dict[str, str]) -> Dict[str, Any]:
    explicit = environ.get("CSE_LICENSE_FILE")
    candidates = (
        [explicit]
        if explicit
        else [
            str(Path(environ.get("HOME", str(Path.home()))) / ".config" / "case-search" / "config.json")
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            return _entry(True, f"license file present at {candidate}")
    return _entry(
        False,
        "no case-search license file; licensed commands fail with rc=4",
        "set CSE_LICENSE_FILE or install ~/.config/case-search/config.json",
    )


def _camofox_url(environ: Dict[str, str]) -> Optional[str]:
    raw = environ.get(CAMOFOX_ENV_VAR)
    return raw.strip() if raw and raw.strip() else None


def _probe_camofox(url: str, timeout: float = 4.0) -> Optional[bool]:
    """Return True/False if the runtime answered, None if it did not respond.

    Any HTTP answer counts as up: the endpoint is a local service whose job is
    to render pages, and its status body is not the question being asked here.
    """
    try:
        with urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            resp.read(1024)
            return True
    except URLError:
        # A 4xx/5xx still proves the listener is there.
        return False
    except Exception:
        return None


def _check_camofox(environ: Dict[str, str]) -> Dict[str, Any]:
    configured = _camofox_url(environ)
    if not configured:
        return _entry(
            False,
            f"{CAMOFOX_ENV_VAR} is not set; JS-capable commands fall back to the "
            "in-process browser and fail",
            f"export {CAMOFOX_ENV_VAR}={DEFAULT_CAMOFOX_URL}",
        )
    answer = _probe_camofox(configured)
    if answer is True:
        return _entry(True, f"camofox runtime reachable at {configured}")
    if answer is False:
        return _entry(
            False,
            f"camofox runtime at {configured} answered with an error status",
            "start it, then re-run this check",
        )
    return _entry(
        False,
        f"camofox runtime at {configured} did not answer",
        "start it, then re-run this check",
    )


def _check_legal_rules() -> Dict[str, Any]:
    """Report the tariff ruleset the engine will actually apply.

    Reading it here proves the engine is usable before an agent spends a
    calculation on it, and pins the exact spec/tariff versions that produced a
    number — a fee computed under 1.0.0 is not the same claim as one under 1.1.0.
    """
    from sud_cli.legal_engine import load_rules

    rules = load_rules()
    if not isinstance(rules, dict):
        return _entry(False, "legal rules file did not parse into a mapping")
    spec = rules.get("spec_version") or rules.get("version") or "unknown"
    tariff = rules.get("tariff_version", "unknown")
    calendar = rules.get("production_calendar") or {}
    transfers = calendar.get("government_transfers_encoded_through", "n/a")
    status = calendar.get("status", "unknown")
    return _entry(
        True,
        f"legal rules spec v{spec} (tariff {tariff}); production calendar "
        f"{status}, government transfers encoded through {transfers}",
    )


def check_live_readiness(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Return a machine-readable readiness report.

    ``ready`` is true only when every blocking check passed. It is a statement
    about configuration, never about whether a given court answers — the tool
    cannot know that without making a request.
    """
    source: Dict[str, str] = dict(environ if environ is not None else os.environ)

    named_checks = {
        "live_access": _check_live_flag(source),
        "case_search_binary": _check_binary(source),
        "license": _check_license(source),
        "camofox_runtime": _check_camofox(source),
        "legal_rules": _check_legal_rules(),
    }
    blocking = [name for name, check in named_checks.items() if not check["ok"]]
    return {
        "ready": not blocking,
        "checks": named_checks,
        "blocked_by": blocking,
        "usage": {
            "case_reference_examples": [
                {"reference": ref, "note": note} for ref, note in CASE_REFERENCE_EXAMPLES
            ],
            "lookup_note": (
                "lookup defaults to case-search's 'synthetic' provider, which "
                "only knows fixture ids — always pass --source kad|mos-gorsud|"
                "mos-gorsud-dom|sudrf"
            ),
            "live_commands": list(LIVE_COMMANDS),
            "global_flags": ["--json must precede the subcommand", "--version"],
        },
    }