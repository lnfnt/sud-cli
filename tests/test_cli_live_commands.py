# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""CLI surface for the native live commands (case-history, sudrf-live, mos-live).

Live sources are core functionality since 07.11.2026 (owner decision): there
is no env gate — the commands reach the native ``case_search`` public API by
default. The tests pin that the arguments reach the API under their own
names, that argparse enforces the required arguments, and that an adapter
failure surfaces as a typed error with the cause (fail-closed by fact).
"""

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest


def _invoke(monkeypatch, argv, env=None):
    import sud_cli.cli as cli_mod

    if env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    code = None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_mod.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
    return code, out.getvalue(), err.getvalue()


def _ok_outcome(**extra):
    payload = {"status": "ok"}
    payload.update(extra)
    return type("Outcome", (), {"to_dict": lambda self: dict(payload)})()


@pytest.mark.parametrize(
    "command,rest",
    [
        ("case-history", ["А40-12345/2026"]),
        ("sudrf-live", ["А40-12345/2026"]),
    ],
)
def test_live_command_runs_by_default(monkeypatch, command, rest):
    """Default-enabled: no env is required, the native API is reached as-is."""
    import sud_cli.case_search.public_api as public_api

    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)
    seen = {}

    def fake_sudrf(case_number=None, *, host=None, **kw):
        seen["case_number"] = case_number
        return _ok_outcome()

    def fake_history(case_number=None, **kw):
        seen["case_number"] = case_number
        return _ok_outcome()

    monkeypatch.setattr(public_api, "get_sudrf_case", fake_sudrf)
    monkeypatch.setattr(public_api, "case_history", fake_history)
    code, out, err = _invoke(monkeypatch, ["--json", command] + rest)
    assert code == 0, err
    assert seen["case_number"] == "А40-12345/2026"
    assert json.loads(out)["status"] == "ok"
    assert "warning" not in out


def test_mos_live_runs_by_default(monkeypatch):
    import sud_cli.case_search.public_api as public_api

    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)
    seen = {}

    def fake_mos(court_slug=None, case_number=None, **kw):
        seen["court_slug"] = court_slug
        seen["case_number"] = case_number
        return _ok_outcome()

    monkeypatch.setattr(public_api, "get_mos_case", fake_mos)
    code, out, err = _invoke(
        monkeypatch, ["--json", "mos-live", "msk", "А40-12345/2026"]
    )
    assert code == 0, err
    assert json.loads(out)["status"] == "ok"


def test_live_command_fails_closed_by_fact(monkeypatch):
    """No network/proxy: the adapter's typed error names the cause, exit non-zero."""
    import sud_cli.case_search.public_api as public_api

    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)

    def failing_history(case_number=None, **kw):
        return type("Outcome", (), {
            "to_dict": lambda self: {"status": "error",
                                     "reason": "network unreachable via proxy"}
        })()

    monkeypatch.setattr(public_api, "case_history", failing_history)
    code, out, err = _invoke(monkeypatch, ["--json", "case-history", "А40-12345/2026"])
    assert code != 0
    assert out == ""
    payload = json.loads(err)
    assert payload["status"] == "error"
    assert "unreachable" in payload["data"]["reason"]


def test_live_command_needs_a_case_id(monkeypatch):
    """A missing case id must be refused by argparse, never guessed."""
    code, _, _ = _invoke(monkeypatch, ["--json", "case-history"])
    assert code != 0


def test_mos_live_needs_both_arguments(monkeypatch):
    code, _, _ = _invoke(monkeypatch, ["--json", "mos-live", "msk"])
    assert code != 0


def test_case_history_forwards_case_id_to_native_api(monkeypatch):
    import sud_cli.case_search.public_api as public_api

    seen = {}

    def fake_history(case_number=None, **kw):
        seen["case_number"] = case_number
        return _ok_outcome()

    monkeypatch.setattr(public_api, "case_history", fake_history)
    code, out, err = _invoke(
        monkeypatch,
        ["--json", "case-history", "А40-12345/2026"],
    )
    assert code == 0, err
    assert seen["case_number"] == "А40-12345/2026"
    assert json.loads(out)["status"] == "ok"


def test_mos_live_forwards_slug_and_case_id(monkeypatch):
    """mos-live needs both the slug and the case id; losing one breaks routing."""
    import sud_cli.case_search.public_api as public_api

    seen = {}

    def fake_mos(court_slug=None, case_number=None, **kw):
        seen["court_slug"] = court_slug
        seen["case_number"] = case_number
        return _ok_outcome()

    monkeypatch.setattr(public_api, "get_mos_case", fake_mos)
    code, out, err = _invoke(
        monkeypatch,
        ["--json", "mos-live", "msk", "А40-12345/2026"],
    )
    assert code == 0, err
    assert seen["court_slug"] == "msk"
    assert seen["case_number"] == "А40-12345/2026"


def test_sudrf_live_forwards_host_default(monkeypatch):
    import sud_cli.case_search.public_api as public_api

    seen = {}

    def fake_sudrf(case_number=None, *, host=None, **kw):
        seen["case_number"] = case_number
        seen["host"] = host
        return _ok_outcome()

    monkeypatch.setattr(public_api, "get_sudrf_case", fake_sudrf)
    code, out, err = _invoke(
        monkeypatch,
        ["--json", "sudrf-live", "А40-12345/2026"],
    )
    assert code == 0, err
    assert seen["case_number"] == "А40-12345/2026"
    assert seen["host"] == "oblsud.mo.sudrf.ru"


def test_removed_bridge_commands_are_unknown(monkeypatch):
    """W8: the transitional bridge commands must not resolve anymore.

    In --json mode the CLI answers with a typed invalid_arguments error (the
    machine contract); the command simply does not exist in the parser.
    """
    for argv in (["case-search"], ["case-search", "providers"]):
        code, out, err = _invoke(monkeypatch, ["--json"] + argv)
        assert code != 0
        payload = json.loads(err)
        assert payload["status"] == "error"
        assert "case-search" in payload["data"]["detail"]
