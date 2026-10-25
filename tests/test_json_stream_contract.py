"""Under --json, every stream must carry typed output — including refusals.

Found by a systematic sweep of all commands: ``fees`` and ``deadlines`` printed
the Russian reference-only notice as bare text on stderr even with --json, so a
caller parsing stderr as JSON got prose where it expected an envelope. The
notice itself is still delivered — inside the successful payload, and on stderr
in text mode — so this is about machine-readability, not about losing it.
"""

import json
import subprocess
import sys

import pytest

SUD_CLI_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _run(argv, env=None):
    """Invoke the CLI as a subprocess, the way an agent would."""
    import os

    base = {k: v for k, v in os.environ.items() if not k.startswith("CSE_")}
    for key in ("HTTP_PROXY", "HTTPS_PROXY"):
        base.pop(key, None)
    if env:
        base.update(env)
    proc = subprocess.run(
        [sys.executable, "-m", "sud_cli", *argv],
        cwd=str(SUD_CLI_ROOT),
        env=base,
        capture_output=True,
        text=True,
        timeout=250,
    )
    return proc


REFUSALS = [
    ["fees", "--jurisdiction", "arbitration", "--kind", "property_claim"],
    ["deadlines", "as_appeal_decision"],
    ["deadlines", "нет_такого_ключа", "--event-date", "2026-01-15"],
]


@pytest.mark.parametrize("argv", REFUSALS, ids=[
    "fees-no-amount", "deadlines-no-date", "deadlines-unknown-key"])
def test_refusal_is_json_on_stderr_under_json_flag(argv):
    proc = _run(["--json"] + argv)
    assert proc.returncode != 0
    assert proc.stdout.strip() == "", "stdout must stay empty on refusal"

    payload = json.loads(proc.stderr)
    assert payload["status"] != "ok", "a refusal must not be reported as success"
    assert payload["data"] is not None or payload.get("meta")


@pytest.mark.parametrize("argv", REFUSALS, ids=[
    "fees-no-amount", "deadlines-no-date", "deadlines-unknown-key"])
def test_refusal_names_the_missing_parameter(argv):
    """The agent needs to know *what* to supply, not just that it failed.

    The typed code lives in ``data.status`` — the envelope's own ``status`` is
    the coarse ok/error split. Both are checked so the contract is pinned.
    """
    proc = _run(["--json"] + argv)
    payload = json.loads(proc.stderr)

    typed = payload["data"]["status"]
    assert typed and typed != "ok", f"no typed refusal code: {typed!r}"
    assert typed != "error", "the refusal must be a specific code, not a bare error"
    assert payload["data"]["detail"], "a refusal must explain itself"

    # And the same code must be machine-readable, not Russian prose only.
    assert typed in proc.stderr


def test_disclaimer_travels_inside_the_json_payload():
    """Suppressing the text notice must not lose the disclaimer."""
    proc = _run([
        "--json", "fees", "--jurisdiction", "arbitration",
        "--kind", "property_claim", "--claim-amount", "1500000",
    ])
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["data"]["disclaimer"], "disclaimer must be in the payload"
    assert "Справочный" in payload["data"]["disclaimer"]


def test_text_mode_still_prints_the_disclaimer_to_stderr():
    """The human-facing notice is not removed, only relocated."""
    proc = _run([
        "fees", "--jurisdiction", "arbitration",
        "--kind", "property_claim", "--claim-amount", "1500000",
    ])
    assert proc.returncode == 0
    assert "Справочный" in proc.stderr


def test_text_mode_refusal_keeps_the_notice():
    proc = _run(["fees", "--jurisdiction", "arbitration", "--kind", "property_claim"])
    assert proc.returncode != 0
    assert "Справочный" in proc.stderr


def test_successful_json_output_is_clean_on_both_streams():
    proc = _run([
        "--json", "fees", "--jurisdiction", "arbitration",
        "--kind", "property_claim", "--claim-amount", "1500000",
    ])
    json.loads(proc.stdout)          # stdout is pure JSON
    assert proc.stderr.strip() == "", (
        "stderr must be empty on success: nothing else belongs on that stream"
    )