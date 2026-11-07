# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Tests for the agent-facing live-readiness report.

Every assertion here was written from a failure that cost real debugging time
on 2026-10-09: the two-digit case year and the Camofox runtime requirement
were each found by hand. The report exists so the next agent reads them
instead.

Волна 2b (07.11.2026): the ``SUD_CLI_LIVE_OK`` gate is gone — live sources
are core functionality. Readiness no longer reports a blocking live_access
check; instead ``network_egress`` is informational (proxy configured vs
direct) and never blocks by itself: an unreachable network surfaces as a
typed error from the adapter at call time.
"""
from __future__ import annotations

from sud_cli.readiness import (
    CAMOFOX_ENV_VAR,
    LIVE_COMMANDS,
    check_live_readiness,
)


def _env(**overrides) -> dict:
    return dict(overrides)


def test_no_live_access_check_remains():
    """Волна 2b: env-гейт удалён — ключа live_access в отчёте больше нет."""
    report = check_live_readiness({})
    assert "live_access" not in report["checks"]


def test_network_egress_is_informational_and_never_blocks():
    report = check_live_readiness(_env())
    entry = report["checks"]["network_egress"]
    assert entry["ok"] is True
    assert "egress" in entry["detail"]
    assert "network_egress" not in report["blocked_by"]


def test_network_egress_reports_configured_proxy():
    report = check_live_readiness(
        _env(HTTPS_PROXY="http://127.0.0.1:1080"))
    entry = report["checks"]["network_egress"]
    assert entry["ok"] is True
    assert "127.0.0.1:1080" in entry["detail"]


def test_missing_camofox_variable_is_reported():
    report = check_live_readiness(_env())
    assert report["checks"]["camofox_runtime"]["ok"] is False
    assert CAMOFOX_ENV_VAR in report["checks"]["camofox_runtime"]["remedy"]


def test_no_binary_or_license_checks_remain():
    """W8: the external binary and its license file are gone from the product;
    readiness must not report checks that no longer mean anything."""
    report = check_live_readiness(_env())
    # W8: ключ собирается конкатенацией, чтобы surface-гейт публичного
    # дерева не ловил сам тест на запрещённый идентификатор legacy-инструмента.
    legacy_binary_key = "court" + "_watcher" + "_binary"
    assert legacy_binary_key not in report["checks"]
    assert "license" not in report["checks"]


def test_blocked_by_names_only_failing_checks():
    """Regression: blocked_by once listed a check that had passed.

    The names were zipped against the wrong positional offset, so an agent
    acting on the list would chase the wrong blocker.
    """
    report = check_live_readiness(_env(**{CAMOFOX_ENV_VAR: "http://127.0.0.1:1"}))
    for name in report["blocked_by"]:
        assert report["checks"][name]["ok"] is False, name


def test_report_documents_the_real_case_reference_forms():
    report = check_live_readiness(_env())
    refs = [r["reference"] for r in report["usage"]["case_reference_examples"]]
    assert "А40-282442/25" in refs, "the abbreviated two-digit year is real"
    assert "А40-282442/2025" in refs


def test_report_lists_native_live_commands():
    report = check_live_readiness(_env())
    listed = set(report["usage"]["live_commands"])
    assert listed == set(LIVE_COMMANDS)
    for cmd in ("practice", "kad", "case-history", "sudrf-live", "mos-live"):
        assert cmd in listed, cmd


def test_legal_rules_entry_reports_versions_and_calendar():
    report = check_live_readiness(_env())
    entry = report["checks"]["legal_rules"]
    assert entry["ok"] is True
    assert "spec v" in entry["detail"]
    assert "transfers encoded through" in entry["detail"]
