"""CLI surface for the positional live commands added in phase 11.

These four commands were reachable through the bridge allowlist all along, but
had no CLI entry point — the bridge could invoke them while the user could not.
The tests below pin that they exist, that they refuse offline, and that they
forward the right arguments.
"""

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest

POSITIONAL_LIVE = [
    ("lookup", ["А40-12345/2026"]),
    ("case-history", ["А40-12345/2026"]),
    ("sudrf-live", ["А40-12345/2026"]),
    ("mos-live", ["msk", "А40-12345/2026"]),
]


def _invoke(monkeypatch, argv):
    import sud_cli.cli as cli_mod

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return {"status": "ok", "data": {"stub": "ok"}}

    monkeypatch.setattr(cli_mod, "run_bridge_command", fake_run)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_mod.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
    return code, captured, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("command,rest", POSITIONAL_LIVE)
def test_positional_live_command_is_wired_to_the_bridge(monkeypatch, command, rest):
    """Each CLI entry must reach the bridge under its own command name."""
    code, captured, _, _ = _invoke(
        monkeypatch,
        ["--json", "case-search", command] + rest,
    )
    assert code == 0
    assert captured["command"] == command
    assert captured["kwargs"]["case_id"] == "А40-12345/2026"


def test_mos_live_forwards_the_court_slug(monkeypatch):
    """mos-live needs both the slug and the case id; losing one breaks routing."""
    code, captured, _, _ = _invoke(
        monkeypatch, ["--json", "case-search", "mos-live", "msk", "А40-12345/2026"]
    )
    assert code == 0
    assert captured["kwargs"]["court_slug"] == "msk"
    assert captured["kwargs"]["case_id"] == "А40-12345/2026"


def test_positional_live_command_needs_a_case_id(monkeypatch):
    """A missing case id must be refused by argparse, never guessed."""
    code, captured, _, _ = _invoke(
        monkeypatch, ["--json", "case-search", "lookup"]
    )
    assert code != 0
    assert captured == {}


def test_mos_live_needs_both_arguments(monkeypatch):
    code, captured, _, _ = _invoke(
        monkeypatch, ["--json", "case-search", "mos-live", "msk"]
    )
    assert code != 0
    assert captured == {}


def test_positional_live_refuses_when_gate_is_closed(monkeypatch):
    """Offline is the default: the refusal must happen before any network use."""
    from sud_cli import case_search_bridge as bridge

    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "")
    monkeypatch.setattr(bridge, "_LIVE_COMMANDS", set(bridge._LIVE_COMMANDS))
    monkeypatch.setattr(
        bridge, "_exec", lambda *a, **k: pytest.fail("must not spawn upstream")
    )
    out, err = io.StringIO(), io.StringIO()
    code = None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            from sud_cli.cli import main

            code = main(["--json", "case-search", "lookup", "А40-12345/2026"])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0

    assert code != 0
    assert out.getvalue() == ""
    payload = json.loads(err.getvalue())
    assert payload["status"] == "blocked", (
        "a closed live gate is not a failure: nothing was attempted, so the "
        "status must say 'blocked' rather than 'error'"
    )
    assert payload["data"] == (
        "live court access is disabled; set SUD_CLI_BRIDGE_LIVE=1 to enable"
    )


def test_positional_live_error_keeps_stdout_empty(monkeypatch):
    """Project convention: in --json mode errors go to stderr, stdout stays empty."""
    import sud_cli.cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "run_bridge_command",
        lambda *a, **k: {"status": "error", "data": "live court command failed"},
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_mod.main(
                ["--json", "case-search", "lookup", "А40-12345/2026"]
            )
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
    assert code != 0
    assert out.getvalue() == ""
    assert json.loads(err.getvalue())["data"] == "live court command failed"