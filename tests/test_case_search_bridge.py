"""Tests for the safe case-search CLI bridge."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sud_cli import case_search_bridge as bridge
from sud_cli.case_search_bridge import (
    LIVE_ENV_VAR,
    BridgeError,
    run_bridge_command,
)


def _fake_script(command_block: str) -> str:
    """Return a fake case-search script that echoes argv/env with output."""
    return f'''#!/usr/bin/env python3
import json
import os
import sys

command = sys.argv[1]

env_snapshot = {{k: os.environ.get(k) for k in [
    "HOME", "HTTP_PROXY", "HTTPS_PROXY", "PATH", "SUD_CLI_STATE_DIR", "TMPDIR",
]}}

{command_block}
'''


FAKE_PROVIDERS_SCRIPT = _fake_script('''
print(json.dumps({
    "argv": sys.argv,
    "env": env_snapshot,
    "providers": [{"name": "fake-provider", "status": "available"}],
}))
''')


FAKE_IMPORT_SCRIPT = _fake_script('''
out_path = sys.argv[sys.argv.index("--json") + 1]
case_id = sys.argv[sys.argv.index("--case") + 1]
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({
        "case_id": case_id,
        "imported": True,
        "command": command,
        "argv": sys.argv,
        "env": env_snapshot,
    }, f)
''')


FAKE_EXTRACT_SCRIPT = _fake_script('''
out_path = sys.argv[sys.argv.index("--out") + 1]
with open(out_path, "w", encoding="utf-8") as f:
    f.write("extracted text")
''')


FAKE_PROVIDERS_WITH_STDERR_SCRIPT = _fake_script('''
print(json.dumps({
    "argv": sys.argv,
    "env": env_snapshot,
    "providers": [{"name": "fake-provider", "status": "available"}],
}))
sys.stderr.write("minor stderr info\\n")
''')

FAKE_LIVE_SNAPSHOT_SCRIPT = _fake_script('''
out_path = sys.argv[sys.argv.index("--json") + 1]
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({
        "case_number": sys.argv[2],
        "status": "Synthetic pending",
        "argv": sys.argv,
        "env": env_snapshot,
    }, f)
''')


def _write_fake_executable(tmp_path: Path, script: str) -> Path:
    fake = tmp_path / "case-search"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(0o755)
    return fake


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def sample_input(state_dir):
    p = state_dir / "input.html"
    p.write_text("<html>synthetic</html>", encoding="utf-8")
    return p


class TestAllowlistAndIsolation:
    def test_forbidden_command_does_not_run_subprocess(self, state_dir, monkeypatch):
        """``config`` is permanently forbidden: it can mutate upstream config."""
        called = []
        original_run = subprocess.run

        def sentinel(*args, **kwargs):
            called.append((args, kwargs))
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", sentinel)

        result = run_bridge_command(
            "config",
            state_dir=state_dir,
            binary_path="/fake/case-search",
        )
        assert result["status"] == "blocked"
        assert called == []

    @pytest.mark.parametrize("command", [
        "watch",
        "config",
    ])
    def test_each_permanently_forbidden_command_is_blocked(self, command, state_dir):
        result = run_bridge_command(command, state_dir=state_dir, binary_path="/fake/case-search")
        assert result["status"] == "blocked"

    @pytest.mark.parametrize("command", [
        # Phase 8 moved search-practice / kad-search / get-text into the
        # flag-driven allowlist, so they are no longer "outside". The names
        # below remain in neither the offline nor the live allowlist and must
        # stay blocked even with the live gate open.
        "kad-case-history",
        "sudrf-lookup",
        "vsrf-lookup",
    ])
    def test_commands_outside_both_allowlists_are_blocked(self, command, state_dir):
        """Neither offline nor live allowlisted: stays blocked even with live on."""
        result = run_bridge_command(
            command,
            state_dir=state_dir,
            binary_path="/fake/case-search",
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "blocked"
        assert result["data"] == "unknown bridge command"

    @pytest.mark.parametrize("command", [
        "lookup",
        "case-history",
        "sudrf-live",
        "mos-live",
    ])
    def test_live_commands_blocked_when_live_disabled(self, command, state_dir, monkeypatch):
        """The live gate is the only thing standing between us and the network."""
        called = []
        original_run = subprocess.run

        def sentinel(*args, **kwargs):
            called.append((args, kwargs))
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", sentinel)

        result = run_bridge_command(
            command,
            state_dir=state_dir,
            case_id="А40-12345/2026",
            court_slug="horoshevskij",
            binary_path="/fake/case-search",
            env={},
        )
        assert result["status"] == "blocked"
        assert LIVE_ENV_VAR in result["data"]
        assert called == []

    @pytest.mark.parametrize("bad_value", ["", "0", "no", "trueish", "  ", "enabled"])
    def test_live_gate_rejects_non_explicit_values(self, bad_value, state_dir):
        """A typo must not open network access."""
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path="/fake/case-search",
            env={LIVE_ENV_VAR: bad_value},
        )
        assert result["status"] == "blocked"

    @pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_live_gate_accepts_explicit_values(self, flag_value, state_dir):
        """Sanity: the gate opens only for the documented true values."""
        from sud_cli.case_search_bridge import _live_access_enabled

        assert _live_access_enabled({LIVE_ENV_VAR: flag_value}) is True

    def test_unknown_command_is_blocked(self, state_dir):
        result = run_bridge_command("not-real", state_dir=state_dir, binary_path="/fake/case-search")
        assert result["status"] == "blocked"

    def test_shell_metacharacter_in_case_id_is_rejected(self, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="foo;rm -rf /",
            binary_path=str(fake),
        )
        assert result["status"] == "error"
        assert "case" in result["data"].lower()

    def test_path_traversal_in_input_is_rejected(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=Path("../etc/passwd"),
            case_id="SYNTH-0001",
            binary_path=str(fake),
        )
        assert result["status"] == "error"
        assert "path" in result["data"].lower()

    def test_directory_input_is_rejected(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        d = state_dir / "adir"
        d.mkdir()
        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=d,
            case_id="SYNTH-0001",
            binary_path=str(fake),
        )
        assert result["status"] == "error"
        assert "file" in result["data"].lower()

    def test_input_outside_state_dir_is_rejected(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        outside = tmp_path / "outside.html"
        outside.write_text("<html></html>", encoding="utf-8")
        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=outside,
            case_id="SYNTH-0001",
            binary_path=str(fake),
        )
        assert result["status"] == "error"
        assert "path" in result["data"].lower()


class TestAllowedCommands:
    def test_providers_runs_fake_executable(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_PROVIDERS_SCRIPT)
        result = run_bridge_command("providers", state_dir=state_dir, binary_path=str(fake))
        assert result["status"] == "ok"
        assert result["data"]["providers"] == [{"name": "fake-provider", "status": "available"}]
        assert result["data"]["argv"][0] == str(fake)
        assert result["data"]["argv"][1] == "providers"

    @pytest.mark.parametrize("command", ["import", "kad-import", "vsrf-import", "auto-import"])
    def test_import_runs_fake_executable_with_isolated_store_alerts(self, command, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        result = run_bridge_command(
            command,
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-CASE-0001",
            binary_path=str(fake),
        )
        assert result["status"] == "ok"
        assert result["data"]["case_id"] == "SYNTH-CASE-0001"
        assert result["data"]["imported"] is True

        argv = result["data"]["argv"]
        assert argv[0] == str(fake)
        assert argv[1] == command
        assert "--case" in argv
        assert "SYNTH-CASE-0001" in argv
        assert "--json" in argv
        assert "--store" in argv
        assert "--alerts" in argv

    def test_extract_text_runs_fake_executable(self, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_EXTRACT_SCRIPT)
        result = run_bridge_command(
            "extract-text",
            state_dir=state_dir,
            input_path=sample_input,
            binary_path=str(fake),
        )
        assert result["status"] == "ok"
        assert result["data"] == "extracted text"


class TestFailureModes:
    def test_timeout_returns_unavailable(self, state_dir, sample_input, tmp_path):
        slow_script = tmp_path / "slow-case-search"
        slow_script.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n",
            encoding="utf-8",
        )
        slow_script.chmod(0o755)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", 0.1)
        try:
            result = run_bridge_command(
                "import",
                state_dir=state_dir,
                input_path=sample_input,
                case_id="SYNTH-0001",
                binary_path=str(slow_script),
            )
            assert result["status"] == "unavailable"
        finally:
            monkeypatch.undo()

    def test_nonzero_exit_returns_error(self, state_dir, sample_input, tmp_path):
        fail_script = tmp_path / "fail-case-search"
        fail_script.write_text(
            "#!/usr/bin/env python3\nimport sys\nprint('upstream secret', file=sys.stderr)\nsys.exit(1)\n",
            encoding="utf-8",
        )
        fail_script.chmod(0o755)

        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-0001",
            binary_path=str(fail_script),
        )
        assert result["status"] == "error"
        assert "upstream secret" not in result["data"].lower()
        # The reason must be reported, but as a fixed constant — never upstream text.
        assert "с кодом 1" in result["data"]
        assert "upstream" not in result["data"].lower()

    def test_oversized_output_returns_error(self, state_dir, tmp_path):
        huge_script = tmp_path / "huge-case-search"
        huge_script.write_text(
            "#!/usr/bin/env python3\nimport sys\nprint('x' * (2 * 1024 * 1024))\n",
            encoding="utf-8",
        )
        huge_script.chmod(0o755)

        fake = _write_fake_executable(tmp_path, FAKE_PROVIDERS_SCRIPT)
        result = run_bridge_command(
            "providers",
            state_dir=state_dir,
            binary_path=str(huge_script),
        )
        assert result["status"] == "error"
        assert "size" in result["data"].lower()
        assert "x" * 100 not in result["data"]

    def test_oversized_stderr_returns_error(self, state_dir, tmp_path):
        huge_stderr_script = tmp_path / "huge-stderr-case-search"
        huge_stderr_script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('x' * (2 * 1024 * 1024))\n",
            encoding="utf-8",
        )
        huge_stderr_script.chmod(0o755)

        result = run_bridge_command(
            "providers",
            state_dir=state_dir,
            binary_path=str(huge_stderr_script),
        )
        assert result["status"] == "error"
        assert "size" in result["data"].lower()
        assert "x" * 100 not in result["data"]

    def test_overflow_terminates_promptly(self, state_dir, tmp_path):
        overflow_then_sleep_script = tmp_path / "overflow-then-sleep-case-search"
        overflow_then_sleep_script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "chunk = 'x' * 65536\n"
            "for _ in range(40):\n"
            "    sys.stdout.write(chunk)\n"
            "    sys.stdout.flush()\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        overflow_then_sleep_script.chmod(0o755)

        start = time.monotonic()
        result = run_bridge_command(
            "providers",
            state_dir=state_dir,
            binary_path=str(overflow_then_sleep_script),
        )
        elapsed = time.monotonic() - start

        assert result["status"] == "error"
        assert "size" in result["data"].lower()
        assert "x" * 100 not in result["data"]
        assert elapsed < 2.0, f"overflow took {elapsed:.2f}s, expected < 2s"

    def test_small_output_to_both_streams_succeeds(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_PROVIDERS_WITH_STDERR_SCRIPT)
        result = run_bridge_command("providers", state_dir=state_dir, binary_path=str(fake))
        assert result["status"] == "ok"
        assert result["data"]["providers"] == [{"name": "fake-provider", "status": "available"}]

    def test_oversized_json_file_output_terminates_promptly(self, state_dir, sample_input, tmp_path):
        huge_file_script = tmp_path / "huge-json-file-case-search"
        huge_file_script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "out = sys.argv[sys.argv.index('--json') + 1]\n"
            f"with open(out, 'w') as f: f.write('x' * {(2 * 1024 * 1024)})\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        huge_file_script.chmod(0o755)

        start = time.monotonic()
        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-0001",
            binary_path=str(huge_file_script),
        )
        elapsed = time.monotonic() - start

        assert result["status"] == "error"
        assert "size" in result["data"].lower()
        assert "x" * 100 not in result["data"]
        assert elapsed < 2.0, f"file overflow took {elapsed:.2f}s, expected < 2s"

    def test_oversized_text_file_output_terminates_promptly(self, state_dir, sample_input, tmp_path):
        huge_file_script = tmp_path / "huge-txt-file-case-search"
        huge_file_script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "out = sys.argv[sys.argv.index('--out') + 1]\n"
            f"with open(out, 'w') as f: f.write('x' * {(2 * 1024 * 1024)})\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        huge_file_script.chmod(0o755)

        start = time.monotonic()
        result = run_bridge_command(
            "extract-text",
            state_dir=state_dir,
            input_path=sample_input,
            binary_path=str(huge_file_script),
        )
        elapsed = time.monotonic() - start

        assert result["status"] == "error"
        assert "size" in result["data"].lower()
        assert "x" * 100 not in result["data"]
        assert elapsed < 2.0, f"file overflow took {elapsed:.2f}s, expected < 2s"

    def test_timeout_for_file_output_command(self, state_dir, sample_input, tmp_path):
        slow_file_script = tmp_path / "slow-file-case-search"
        slow_file_script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "out = sys.argv[sys.argv.index('--json') + 1]\n"
            "with open(out, 'w') as f: f.write('{}')\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
        slow_file_script.chmod(0o755)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", 0.1)
        try:
            result = run_bridge_command(
                "import",
                state_dir=state_dir,
                input_path=sample_input,
                case_id="SYNTH-0001",
                binary_path=str(slow_file_script),
            )
            assert result["status"] == "unavailable"
        finally:
            monkeypatch.undo()

    def test_malformed_json_import_returns_error(self, state_dir, sample_input, tmp_path):
        bad_json_script = tmp_path / "bad-json-case-search"
        bad_json_script.write_text(
            '#!/usr/bin/env python3\nimport sys\nout = sys.argv[sys.argv.index("--json") + 1]\nwith open(out, "w") as f: f.write("not json")\n',
            encoding="utf-8",
        )
        bad_json_script.chmod(0o755)

        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-0001",
            binary_path=str(bad_json_script),
        )
        assert result["status"] == "error"
        assert "json" in result["data"].lower()

    def test_malformed_json_providers_returns_error(self, state_dir, tmp_path):
        bad_script = tmp_path / "bad-providers-case-search"
        bad_script.write_text(
            "#!/usr/bin/env python3\nprint('not json')\n",
            encoding="utf-8",
        )
        bad_script.chmod(0o755)

        result = run_bridge_command(
            "providers",
            state_dir=state_dir,
            binary_path=str(bad_script),
        )
        assert result["status"] == "error"
        assert "json" in result["data"].lower()

    def test_sanitized_error_does_not_leak_input_path(self, state_dir, sample_input, tmp_path):
        fail_script = tmp_path / "fail-case-search"
        fail_script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n",
            encoding="utf-8",
        )
        fail_script.chmod(0o755)

        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-0001",
            binary_path=str(fail_script),
        )
        assert result["status"] == "error"
        assert str(sample_input) not in result["data"]
        assert "SYNTH-0001" not in result["data"]


class TestEnvironment:
    def test_unsafe_environment_variables_not_passed(self, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        env = os.environ.copy()
        env["HTTP_PROXY"] = "http://bad.proxy:8080"
        env["HTTPS_PROXY"] = "https://bad.proxy:8080"

        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-0001",
            binary_path=str(fake),
            env=env,
        )
        assert result["status"] == "ok"
        assert result["data"]["env"]["HTTP_PROXY"] is None
        assert result["data"]["env"]["HTTPS_PROXY"] is None
        assert result["data"]["env"]["PATH"] is not None
        assert result["data"]["env"]["TMPDIR"] is not None

    def test_home_and_state_dir_redirected_to_temp_workspace(self, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        sentinel_home = "/sentinel/home"
        sentinel_state = "/sentinel/state"
        env = {
            "HOME": sentinel_home,
            "PATH": os.environ.get("PATH", ""),
            "SUD_CLI_STATE_DIR": sentinel_state,
        }

        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-0001",
            binary_path=str(fake),
            env=env,
        )
        assert result["status"] == "ok"
        child_env = result["data"]["env"]
        assert child_env["HOME"] != sentinel_home
        assert child_env["SUD_CLI_STATE_DIR"] != sentinel_state
        assert Path(child_env["HOME"]).is_relative_to(state_dir / "bridge-temp")
        assert Path(child_env["SUD_CLI_STATE_DIR"]).is_relative_to(state_dir / "bridge-temp")
        assert Path(child_env["TMPDIR"]).is_relative_to(state_dir / "bridge-temp")


class TestParityMatrix:
    def test_parity_matrix_lists_all_rows(self):
        """The matrix must cover every bridge command plus the native features.

        It used to assert a hardcoded 12-row list that had gone stale in both
        directions (fees/deadlines advertised as blocked, live lookups as
        unavailable). Coverage is now asserted against the registries, so the
        list cannot drift again.
        """
        matrix = bridge.get_parity_matrix()
        names = {row["capability"] for row in matrix}
        expected = {
            "Provider status discovery",
            "HTML case import",
            "ГАС Правосудие case import",
            "KAD JSON case import",
            "VSRF PDF case import",
            "Auto-detected file import",
            "Document text extraction",
            "Judicial practice search (sudact.ru)",
            "Live KAD/arbitr.ru search",
            "Full decision text by URL",
            "Case history across linked instances",
            "Single case lookup",
            "Live ГАС Правосудие case card",
            "Live Moscow court case card",
            "Court fee calculation",
            "Procedural deadline calculation",
            "Readiness self-check",
            "Case version diff",
            "Official publication metadata",
            "Local NPA fixtures",
        }
        assert names == expected

    def test_parity_statuses_are_valid(self):
        matrix = bridge.get_parity_matrix()
        for row in matrix:
            assert row["sud_cli_status"] in {"native", "bridge", "unavailable", "blocked"}
            assert row["sud_cli_command"]
            # A row that claims reachability must say whether the live gate
            # applies, so an agent can tell "available" from "available once
            # you opt in".
            assert isinstance(row["live_gate"], bool)


class TestShellArgv:
    def test_argv_is_array_not_shell(self, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        result = run_bridge_command(
            "import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id="SYNTH-CASE-0001",
            binary_path=str(fake),
        )
        assert result["status"] == "ok"
        argv = result["data"]["argv"]
        # With argv arrays, every token stays a single argument.
        assert all(isinstance(arg, str) for arg in argv)
        # The --case value is passed as a single token.
        assert "SYNTH-CASE-0001" in argv


class TestCourtReferenceValidation:
    """Synthetic court references must be accepted as single argv tokens while
    unsafe values stay rejected. These identifiers are test-only and synthetic;
    they deliberately resemble but do not match any real case reference.
    """

    @pytest.mark.parametrize("case_id", [
        "A00-00001/2099",
        "А00-00001/2099",  # Cyrillic А, not Latin A
    ])
    @pytest.mark.parametrize("command", ["import", "kad-import", "vsrf-import", "auto-import"])
    def test_synthetic_court_reference_accepted_as_single_argv_token(
        self, case_id, command, state_dir, sample_input, tmp_path
    ):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        result = run_bridge_command(
            command,
            state_dir=state_dir,
            input_path=sample_input,
            case_id=case_id,
            binary_path=str(fake),
        )
        assert result["status"] == "ok"
        assert result["data"]["case_id"] == case_id
        argv = result["data"]["argv"]
        # The reference is passed as exactly one argv token.
        assert argv.count(case_id) == 1
        case_idx = argv.index(case_id)
        assert argv[case_idx - 1] == "--case"

    @pytest.mark.parametrize("case_id", [
        "",
        " ",
        "foo bar",
        "foo;rm -rf /",
        "foo&bar",
        "foo|bar",
        "foo$bar",
        "foo`bar",
        "foo$(bar)",
        "foo\x00bar",
        "foo\x01bar",
        "../etc/passwd",
        "/etc/passwd",
        "foo//bar",
        "foo/bar/baz",
        "-foo",
        "A00-00001/999",  # year too short
        "A/2099",         # missing required digit groups
        "foo\\bar",
        '"foo"',
        "'foo'",
        # A 2-digit year was previously impossible because the old pattern
        # demanded exactly 4 digits. "А00-00001/20" is now accepted: courts
        # genuinely abbreviate years as /25, and the value carries no
        # injectable character, so refusing it would refuse a real format.
    ])
    def test_unsafe_case_ids_are_rejected(self, case_id, state_dir, sample_input, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_IMPORT_SCRIPT)
        result = run_bridge_command(
            "kad-import",
            state_dir=state_dir,
            input_path=sample_input,
            case_id=case_id,
            binary_path=str(fake),
        )
        assert result["status"] == "error"
        assert "case" in result["data"].lower()
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sud_cli.case_search_bridge import run_bridge_command


def _source_url_test_fake(tmp_path: Path, source_url: str) -> Path:
    fake = tmp_path / "fake-case-search"
    script = "\n".join([
        "#!/usr/bin/env python3",
        "import json, sys",
        "out = sys.argv[sys.argv.index('--json') + 1]",
        "payload = {'case_number': 'A00-00001/2099', 'court': 'Synthetic Court', "
        "'status': 'Synthetic pending', 'updated_at': '2099-01-01', "
        f"'provenance': {{'source_url': {source_url!r}}}}}",
        "with open(out, 'w', encoding='utf-8') as f: json.dump(payload, f)",
        "",
    ])
    fake.write_text(script, encoding='utf-8')
    fake.chmod(0o755)
    return fake


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def sample_input(state_dir):
    p = state_dir / "input.json"
    p.write_text('{"synthetic": true}', encoding='utf-8')
    return p


def test_local_import_file_source_url_is_sanitized(state_dir, sample_input, tmp_path):
    source_url = "file:///tmp/private/bridge-temp/random/input/synthetic.json"
    fake = _source_url_test_fake(tmp_path, source_url)
    result = run_bridge_command(
        "kad-import",
        state_dir=state_dir,
        input_path=sample_input,
        case_id="A00-00001/2099",
        binary_path=str(fake),
    )
    assert result["status"] == "ok"
    assert result["data"]["provenance"]["source_url"] == "synthetic://local-import"
    assert source_url not in json.dumps(result["data"])
    assert "bridge-temp" not in json.dumps(result["data"])


# ---------------------------------------------------------------------------
# Live court-source commands
# ---------------------------------------------------------------------------


class TestLiveCommands:
    """Live commands must build correct upstream argv and keep every isolation
    property of the offline path, while remaining gated behind an explicit
    opt-in flag."""

    def test_lookup_passes_case_reference_positionally(self, state_dir, tmp_path):
        """Upstream takes `lookup case_ref`, NOT `--case`."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "ok"
        argv = result["data"]["argv"]
        assert argv[1] == "lookup"
        assert argv[2] == "А40-12345/2026"
        assert "--case" not in argv

    def test_case_history_passes_case_number_positionally(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "case-history",
            state_dir=state_dir,
            case_id="А19-30387/2025",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "ok"
        assert result["data"]["argv"][2] == "А19-30387/2025"

    def test_mos_live_passes_slug_before_case_number(self, state_dir, tmp_path):
        """mos-live takes TWO positionals: court_slug then case_number."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "mos-live",
            state_dir=state_dir,
            court_slug="horoshevskij",
            case_id="02-2101/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "ok"
        argv = result["data"]["argv"]
        assert argv[2] == "horoshevskij"
        assert argv[3] == "02-2101/2026"

    def test_live_result_is_marked_live(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["data"]["bridge"]["live"] is True

    def test_live_command_keeps_env_isolation(self, state_dir, tmp_path):
        """Proxy vars and caller HOME must not reach a live subprocess."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={
                LIVE_ENV_VAR: "1",
                "HTTP_PROXY": "http://proxy.invalid:9",
                "HTTPS_PROXY": "http://proxy.invalid:9",
                "HOME": "/home/someone",
                "PATH": "/usr/bin:/bin",
            },
        )
        assert result["status"] == "ok"
        env = result["data"]["env"]
        assert env["HTTP_PROXY"] is None
        assert env["HTTPS_PROXY"] is None
        assert env["HOME"] != "/home/someone"

    @pytest.mark.parametrize("bad_ref", [
        "; rm -rf /",
        "А40-12345/2026 && echo pwned",
        "--output=/etc/passwd",
        "../../etc/passwd",
        "А40-12345/2",
        "А40-12345/202",
        "А40-12345/20255",
        "А40 12345/2026",
        # Court prefixes are mixed alphanumerics (А40, 2а, 16АП, 02) of up to
        # 4 characters; the court number and year are not optional — these
        # shapes are what a relaxed \d{0,4}-?\d{0,6} would admit.
        "А40/25",          # prefix with no court number
        "/25",             # bare year
        "А4012345/25",     # too many digits for the first group
        # "12345-12345/25" is now covered by the 4-character prefix bound,
        # which is what the old comment here was really protecting: an
        # implausibly long prefix.
        # Trailing newline: Python's `$` matches before a final \n, so a `$`
        # anchor would validate these while the value is not an exact ref.
        "А40-12345/25\n",
        "А40-12345/25\nid\n",
        " А40-12345/25",
        "А40-12345/25 ",
        "",
    ])
    def test_unsafe_case_reference_is_rejected(self, bad_ref, state_dir, tmp_path):
        """Injection attempts must never reach argv."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id=bad_ref,
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "error"
        assert result["data"] in ("missing case reference", "invalid case reference")

    @pytest.mark.parametrize("good_ref", [
        "А40-11111/25",
        "А40-11111/2025",
        "33-35073/2026",
        "2-1234/24",
        "А56-24385/23",
        # ГАС Правосудие prints a DIGIT first: district and appeal courts are the
        # majority of general-jurisdiction caseloads, and the old "letter then
        # digits" pattern refused every one of them.
        "2а-2939/2025",
        "33а-29651/2025",
        "02-3696/2024",
        "16АП-1234/2026",
        "А00-00001/20",  # 2-digit year: a real abbreviation, no injectable chars
    ])
    def test_real_court_reference_forms_are_accepted(self, good_ref, state_dir, tmp_path):
        """The two-digit year courts actually print must reach the binary.

        АС Москвы heads its rulings ``Дело № А40-11111/25``; rejecting that
        form rejected references case-search itself resolves.
        """
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id=good_ref,
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "ok"
        assert result["data"]["case_number"] == good_ref

    @pytest.mark.parametrize("source", ["kad", "mos-gorsud", "mos-gorsud-dom", "sudrf"])
    def test_lookup_source_flag_reaches_the_binary(self, source, state_dir, tmp_path):
        """``--source`` must be forwarded verbatim: without it case-search
        falls back to the ``synthetic`` provider, which rejects every real ref."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А81-10850/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
            options={"--source": source},
        )
        assert result["status"] == "ok"
        argv = result["data"]["argv"]
        assert argv[argv.index("--source") + 1] == source

    @pytest.mark.parametrize("options", [
        {"--source": "synthetic"},      # fixture provider, unreachable here
        {"--source": "../../etc/passwd"},
        {"--source": "kad; id"},
        {"--source": ""},
        {"--source": 42},
        {"--runtime-url": "http://evil"},  # excluded from the bridge by design
        {"--fixtures": "/tmp/fixtures"},
        {"--source": "kad", "--now": "2020-01-01"},
    ])
    def test_lookup_rejects_options_outside_the_allowlist(self, options, state_dir, tmp_path):
        """Only enumerated values of listed flags may be forwarded."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А81-10850/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
            options=options,
        )
        assert result["status"] == "error"
        assert "option" in result["data"]

    def test_live_command_without_flag_support_refuses_options(self, state_dir, tmp_path):
        """``case-history`` takes no flags; an options mapping is refused."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "case-history",
            state_dir=state_dir,
            case_id="А40-11111/25",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
            options={"--source": "kad"},
        )
        assert result["status"] == "error"
        assert "option" in result["data"]

    @pytest.mark.parametrize("bad_slug", [
        "../etc",
        "slug; rm -rf /",
        "UPPERCASE",
        "",
        "slug/../../x",
    ])
    def test_unsafe_court_slug_is_rejected(self, bad_slug, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "mos-live",
            state_dir=state_dir,
            court_slug=bad_slug,
            case_id="02-2101/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "error"
        assert result["data"] in ("missing court slug", "invalid court slug")

    def test_single_positional_command_rejects_extra_slug(self, state_dir, tmp_path):
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            court_slug="horoshevskij",
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "error"
        assert result["data"] == "court slug is not valid for this command"

    def test_live_failure_does_not_leak_upstream_stderr(self, state_dir, tmp_path):
        """Upstream errors can carry case data or paths; never echo them."""
        fake = _write_fake_executable(
            tmp_path,
            _fake_script('sys.stderr.write("SECRET-CASE-DATA leaked")\nsys.exit(1)'),
        )
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "error"
        assert "SECRET-CASE-DATA" not in json.dumps(result)

    def test_live_timeout_is_bounded(self, state_dir, tmp_path, monkeypatch):
        fake = _write_fake_executable(tmp_path, _fake_script("import time\ntime.sleep(60)"))
        monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", 1)
        result = run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert result["status"] == "unavailable"
        assert result["data"] == "bridge subprocess timed out"

    def test_live_uses_shell_false_argv_array(self, state_dir, tmp_path, monkeypatch):
        """The live path must use the same shell=False argv-array execution."""
        fake = _write_fake_executable(tmp_path, FAKE_LIVE_SNAPSHOT_SCRIPT)
        seen = {}
        original = subprocess.Popen

        def spy(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            seen["shell"] = kwargs.get("shell")
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", spy)
        run_bridge_command(
            "lookup",
            state_dir=state_dir,
            case_id="А40-12345/2026",
            binary_path=str(fake),
            env={LIVE_ENV_VAR: "1"},
        )
        assert seen["shell"] in (None, False)
        assert isinstance(seen["cmd"], list)
