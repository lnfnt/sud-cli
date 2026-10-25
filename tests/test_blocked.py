"""Invocation-shape and isolation contracts for the fees/deadlines calculators.

These commands were stubs for the whole of Phase 1 and are now live (spec v1.0.0
approved by colleague-legal on 2026-10-08). The "must stay blocked forever" assertions
are gone because the spec now approves them, but the safety properties those
tests were actually protecting are unchanged and are re-asserted here:

* a call missing a legally-required parameter must be **refused**, never
  answered with a number — the calculator fails closed, it does not guess;
* output must never reveal the user's home directory;
* neither calculator may shell out; subprocess usage stays confined to the
  explicit case-search bridge.
"""

import json
from pathlib import Path

import pytest

from tests.conftest import run_cli


# Shapes that omit or mistype a legally-required parameter. Each must be
# refused: exit non-zero and no amount/date in the payload.
BAD_FEES_SHAPES = [
    ["fees"],
    ["fees", "--json"],
    ["fees", "100"],
    ["fees", "--amount", "10000", "--court", "district"],
    ["--json", "fees"],
    ["--json", "fees", "--whatever"],
    ["fees", "--jurisdiction", "arbitration"],  # missing --kind
    ["fees", "--kind", "property_claim"],  # missing --jurisdiction
    ["fees", "--jurisdiction", "tribunal", "--kind", "appeal", "--applicant", "entity"],
    ["fees", "--jurisdiction", "arbitration", "--kind", "appeal", "--applicant", "entity",
     "--claim-amount", "notanumber"],
]

BAD_DEADLINES_SHAPES = [
    ["deadlines"],
    ["deadlines", "--json"],
    ["deadlines", "2026-01-01"],
    ["deadlines", "2026-01-01", "--appeal"],
    ["--json", "deadlines"],
    ["--json", "deadlines", "2026-01-01", "--service-method", "mail"],
    ["deadlines", "as_appeal_decision"],  # missing --event-date
    ["deadlines", "as_appeal_decision", "--event-date", "09.10.2026"],  # bad format
    ["deadlines", "as_appeal_decision", "--event-date", "2026-02-30"],  # impossible date
]


@pytest.mark.parametrize("args", BAD_FEES_SHAPES)
def test_fees_refuses_incomplete_or_invalid_shapes(args):
    code, out, err = run_cli(args)
    assert code != 0, f"must refuse {args}, not answer them"


@pytest.mark.parametrize("args", BAD_DEADLINES_SHAPES)
def test_deadlines_refuses_incomplete_or_invalid_shapes(args):
    code, out, err = run_cli(args)
    assert code != 0, f"must refuse {args}, not answer them"


@pytest.mark.parametrize("args", BAD_FEES_SHAPES)
def test_refused_fees_never_emits_an_amount(args):
    """A refused call must not leak a plausible-looking number."""
    code, out, err = run_cli(args)
    blob = out + err
    assert "amount_rub" not in blob


@pytest.mark.parametrize("args", BAD_DEADLINES_SHAPES)
def test_refused_deadlines_never_emits_a_date(args):
    code, out, err = run_cli(args)
    blob = out + err
    assert '"deadline"' not in blob


def test_fees_json_mode_has_stable_contract():
    """A refused --json call must still be parseable machine output.

    Per the codebase convention (see test_cli.py), JSON-mode errors go to
    stderr and stdout stays empty.
    """
    code, out, err = run_cli(["--json", "fees", "--kind", "appeal"])
    assert code != 0
    assert out == ""
    data = json.loads(err)
    assert data["status"] in {"blocked", "error"}


def test_working_fees_call_succeeds_and_is_json_clean():
    """The approved shape must work — guards against over-blocking."""
    code, out, err = run_cli(
        ["--json", "fees", "--jurisdiction", "arbitration",
         "--kind", "property_claim", "--claim-amount", "1000000"]
    )
    assert code == 0
    data = json.loads(out)
    assert data["data"]["status"] == "ok"
    assert data["data"]["amount_rub"] == 55000


def test_working_deadlines_call_succeeds():
    code, out, err = run_cli(
        ["--json", "deadlines", "as_appeal_decision", "--event-date", "2026-10-09"]
    )
    assert code == 0
    data = json.loads(out)
    assert data["data"]["status"] == "ok"
    # 09.10.2026 event -> +1 month on the event day number = 09.11.2026 (Mon).
    assert data["data"]["deadline"] == "2026-11-09"


def test_disclaimer_always_printed_on_success():
    """The spec makes the reference-only disclaimer mandatory on every result."""
    code, out, err = run_cli(
        ["fees", "--jurisdiction", "arbitration", "--kind", "property_claim",
         "--claim-amount", "1000000"]
    )
    assert code == 0
    assert "Справочный расчет" in err
    code, out, err = run_cli(["deadlines", "as_appeal_decision", "--event-date", "2026-10-09"])
    assert code == 0
    assert "Справочный расчет" in err


def test_open_questions_surface_in_deadline_meta():
    """Resolved spec questions stay visible to a caller, not buried in source."""
    code, out, err = run_cli(
        ["--json", "deadlines", "as_appeal_decision", "--event-date", "2026-10-09"]
    )
    data = json.loads(out)
    meta = data.get("meta", {})
    open_q = meta.get("open_questions", {})
    assert any(key.startswith("B") for key in open_q)
    # Б5 was closed by the colleague-legal opinion of 2026-10-08, so no entry may
    # still claim to be unresolved.
    assert not any("UNRESOLVED" in v for v in open_q.values())
    assert "RESOLVED" in open_q["B5_monthly_base"]


def test_fees_output_does_not_expose_home_path(tmp_path, monkeypatch):
    """Calculator output must never reveal the user's home directory path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("SUD_CLI_STATE_DIR", raising=False)
    for args in (
        ["fees", "--jurisdiction", "arbitration", "--kind", "property_claim",
         "--claim-amount", "1000000"],
        ["--json", "fees", "--jurisdiction", "arbitration", "--kind", "property_claim",
         "--claim-amount", "1000000"],
        ["deadlines", "as_appeal_decision", "--event-date", "2026-10-09"],
        ["fees"],  # refused shape must not leak either
    ):
        code, out, err = run_cli(args)
        assert str(fake_home) not in out + err


def test_product_subprocess_is_isolated_to_bridge():
    """Subprocess usage is allowed only in the explicit bridge module."""
    package_root = Path(__file__).parent.parent / "sud_cli"
    for source in package_root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        if source.name == "case_search_bridge.py":
            # The bridge must use argv arrays, shell=False, and bounded timeouts.
            assert ("subprocess.run" in text or "subprocess.Popen" in text), "bridge must use subprocess"
            assert "shell=False" in text, "bridge must set shell=False"
            assert "timeout=" in text, "bridge must set a timeout"
            continue
        assert "subprocess" not in text, f"subprocess found in {source}"
        assert "os.system" not in text, f"os.system found in {source}"
        assert "os.popen" not in text, f"os.popen found in {source}"


def test_calculators_do_not_call_another_process(tmp_path, monkeypatch):
    """Neither calculator may shell out — the tariff engine is pure arithmetic."""
    import subprocess

    called = []
    original_call = subprocess.run

    def sentinel(*args, **kwargs):
        called.append((args, kwargs))
        return original_call(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", sentinel)
    run_cli(["fees", "--jurisdiction", "arbitration", "--kind", "property_claim",
             "--claim-amount", "1000000"])
    run_cli(["deadlines", "as_appeal_decision", "--event-date", "2026-10-09"])
    assert called == []
