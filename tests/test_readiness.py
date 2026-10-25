"""Tests for the agent-facing live-readiness report.

Every assertion here was written from a failure that cost real debugging time
on 2026-10-09: the two-digit case year, the missing ``--source``, and the
Camofox runtime requirement were each found by hand. The report exists so the
next agent reads them instead.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sud_cli.readiness import (
    CAMOFOX_ENV_VAR,
    LIVE_ENV_VAR,
    check_live_readiness,
)


def _env(tmp_path: Path, **overrides) -> dict:
    """A PATH containing a real executable, plus an explicit license file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    binary = bin_dir / "case-search"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    lic = tmp_path / "config.json"
    lic.write_text(json.dumps({"license_key": "x"}), encoding="utf-8")

    base = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "CSE_LICENSE_FILE": str(lic),
        LIVE_ENV_VAR: "1",
    }
    base.update(overrides)
    return base


def test_ready_is_false_when_live_access_is_missing(tmp_path):
    env = _env(tmp_path)
    del env[LIVE_ENV_VAR]
    report = check_live_readiness(env)
    assert report["ready"] is False
    assert "live_access" in report["blocked_by"]
    assert report["checks"]["live_access"]["remedy"]


def test_non_enabling_live_value_does_not_count(tmp_path):
    for value in ("", "0", "false", "no", "maybe", "2"):
        env = _env(tmp_path, **{LIVE_ENV_VAR: value})
        report = check_live_readiness(env)
        assert report["checks"]["live_access"]["ok"] is False, value


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_enabling_live_values_are_recognised(tmp_path, value):
    env = _env(tmp_path, **{LIVE_ENV_VAR: value})
    assert check_live_readiness(env)["checks"]["live_access"]["ok"] is True


def test_missing_binary_is_reported(tmp_path):
    env = _env(tmp_path, PATH="")
    report = check_live_readiness(env)
    assert report["checks"]["case_search_binary"]["ok"] is False
    assert "PATH" in report["checks"]["case_search_binary"]["remedy"]


def test_missing_license_is_reported(tmp_path):
    env = _env(tmp_path, CSE_LICENSE_FILE=str(tmp_path / "absent.json"))
    report = check_live_readiness(env)
    assert report["checks"]["license"]["ok"] is False


def test_missing_camofox_variable_is_reported(tmp_path):
    env = _env(tmp_path)
    report = check_live_readiness(env)
    assert report["checks"]["camofox_runtime"]["ok"] is False
    assert CAMOFOX_ENV_VAR in report["checks"]["camofox_runtime"]["remedy"]


def test_blocked_by_names_only_failing_checks(tmp_path):
    """Regression: blocked_by once listed a check that had passed.

    The names were zipped against the wrong positional offset, so an agent
    acting on the list would chase the wrong blocker.
    """
    env = _env(tmp_path, **{CAMOFOX_ENV_VAR: "http://127.0.0.1:1"})
    report = check_live_readiness(env)
    for name in report["blocked_by"]:
        assert report["checks"][name]["ok"] is False, name
    assert "case_search_binary" not in report["blocked_by"]


def test_report_documents_the_real_case_reference_forms(tmp_path):
    env = _env(tmp_path)
    report = check_live_readiness(env)
    refs = [r["reference"] for r in report["usage"]["case_reference_examples"]]
    assert "А40-11111/25" in refs, "the abbreviated two-digit year is real"
    assert "А40-11111/2025" in refs


def test_report_warns_that_lookup_needs_a_source(tmp_path):
    report = check_live_readiness(_env(tmp_path))
    assert "synthetic" in report["usage"]["lookup_note"]
    assert "--source" in report["usage"]["lookup_note"]


def test_legal_rules_entry_reports_versions_and_calendar(tmp_path):
    report = check_live_readiness(_env(tmp_path))
    entry = report["checks"]["legal_rules"]
    assert entry["ok"] is True
    assert "spec v" in entry["detail"]
    assert "transfers encoded through" in entry["detail"]