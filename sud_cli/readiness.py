# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Live-readiness self-check for AI agents driving sud-cli.

An agent that only reads ``--help`` cannot tell that live court commands need
network egress (direct or via the standard ``HTTPS_PROXY``/``HTTP_PROXY``
variables) or that the case reference uses a two-digit year. Each of those was
discovered by hand on 2026-10-09, at the cost of a full debugging session.
This module answers the same questions in one call, without contacting any
court host.

Live sources are core functionality (owner decision 2026-11-07): they run by
default and fail closed by fact — when the network or proxy is unreachable the
adapters return a typed error naming the cause, never a silent empty success.
There is no environment gate to enable them; the checks here only inspect
configuration and the optional Camofox runtime on loopback. No court request
is made, so the check is safe to run before every session.

``check_live_readiness`` returns a mapping with one entry per concern, each
carrying ``ok``, ``detail`` and — where it applies — ``remedy``, so an agent
can act on the output without reading the source.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional
from urllib.request import urlopen
from urllib.error import URLError

#: Environment variable that routes the embedded browser at an external
#: Camofox runtime. Required on this host: the in-process browser cannot
#: initialise everywhere, so JS-capable commands fail without it.
CAMOFOX_ENV_VAR = "CW_CAMOFOX_URL"

#: Default loopback Camofox endpoint used when the variable is unset.
DEFAULT_CAMOFOX_URL = "http://127.0.0.1:9377"

#: Real court references, covering each prefix shape the live commands accept.
#: Prefixes are mixed alphanumerics of up to 4 characters and letters are legal
#: in ANY position — ГАС Правосудие prints ``2а-2939/2025`` with a digit first.
#: These are examples for the agent, not a validation contract.
CASE_REFERENCE_EXAMPLES = (
    ("А40-282442/25", "АС Москвы — the abbreviated two-digit year courts print"),
    ("А40-282442/2025", "same case, four-digit year"),
    ("33-35073/2026", "SudRF, no letter prefix"),
    ("2а-2939/2025", "ГАС Правосудие — district court, digit before letter"),
    ("16АП-12345/2025", "appellate instance, letter suffix"),
)

#: Native commands that reach an external court host.
LIVE_COMMANDS = (
    "practice",
    "kad",
    "case-history",
    "sudrf-live",
    "mos-live",
    "extract-text",
)


def _entry(ok: bool, detail: str, remedy: str = "") -> Dict[str, Any]:
    entry: Dict[str, Any] = {"ok": ok, "detail": detail}
    if remedy:
        entry["remedy"] = remedy
    return entry


def _check_network_egress(environ: Dict[str, str]) -> Dict[str, Any]:
    """Report how live commands will reach the court hosts.

    Live sources are core functionality since 2026-11-07 (owner decision):
    there is no opt-in gate. Egress goes through the standard
    ``HTTPS_PROXY``/``HTTP_PROXY`` variables when set (a user option; in the
    the project deploy Amnezia intercepts the traffic transparently) and falls back
    to a direct connection otherwise. The check is informational and never
    blocks: an unreachable network or proxy surfaces as a typed error from
    the adapter itself, with the cause named — fail-closed by fact.
    """
    proxy = (
        environ.get("HTTPS_PROXY") or environ.get("https_proxy")
        or environ.get("HTTP_PROXY") or environ.get("http_proxy")
    )
    if proxy:
        return _entry(True, f"egress configured via proxy {proxy} (HTTPS_PROXY/HTTP_PROXY)")
    return _entry(
        True,
        "direct egress (HTTPS_PROXY/HTTP_PROXY not set); live commands are "
        "enabled by default and fail with an explicit error when the network "
        "is unreachable",
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
        "network_egress": _check_network_egress(source),
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
            "live_commands": list(LIVE_COMMANDS),
            "global_flags": ["--json must precede the subcommand", "--version"],
        },
    }
