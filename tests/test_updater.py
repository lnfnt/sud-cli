# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""tests/test_updater.py — the task brief.md (offline, runner injected)."""
import json

import pytest

from sud_cli import updater


def make_runner(returncode=0, stdout="", stderr=""):
    calls = []

    def runner(cmd, timeout=None, capture_output=True, text=True):
        calls.append({"cmd": cmd, "timeout": timeout})
        return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()

    runner.calls = calls
    return runner


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    return tmp_path


def test_check_pypi_update_available(state_dir):
    runner = make_runner(returncode=0,
                         stdout=[f"packages found: {updater.PACKAGE}",
                                 f"Available versions: 99.0.0, {updater.__version__}, 1.0.0"][1])
    result = updater.check_update(source="pypi", runner=runner)
    assert result["current_version"] == updater.__version__
    assert result["latest_version"] == "99.0.0"
    assert result["update_available"] is True
    assert result["data_safe"] is True
    assert result["state_dir"] == str(state_dir)
    assert runner.calls[0]["cmd"][:3] == [updater.sys.executable, "-m", "pip"]


def test_check_git_uses_ls_remote_and_target_default(state_dir):
    runner = make_runner(returncode=0, stdout="abc123 refs/heads/main")
    result = updater.check_update(source="git", runner=runner)
    assert runner.calls[0]["cmd"] == ["git", "ls-remote", updater.GIT_URL, "HEAD"]
    # sha не равен номеру версии пакета -> обновление считается доступным
    assert result["update_available"] is True
    assert result["data_safe"] is True


def test_check_companion(state_dir):
    # latest == current -> update_available False
    runner = make_runner(returncode=0, stdout=f"Available versions: {updater.__version__}, 1.0.0")
    assert updater.check_update(source="pypi", runner=runner)["update_available"] is False
    # pip error -> latest None, update_available False (fail-closed, без ответа ok-on-eye)
    runner = make_runner(returncode=1, stderr="no network")
    r = updater.check_update(source="pypi", runner=runner)
    assert r["latest_version"] is None and r["update_available"] is False


def test_run_update_success_mocks_pip(state_dir):
    runner = make_runner(returncode=0, stdout="Successfully installed")
    result = updater.run_update(source="pypi", runner=runner)
    pip_cmd = runner.calls[0]["cmd"]
    assert [updater.sys.executable, "-m", "pip", "install"] == pip_cmd[:4]
    assert "--upgrade" in pip_cmd
    assert result["ok"] is True
    assert result["status"] in ("updated", "already_latest")
    assert result["data_preserved"] is True
    assert result["state_dir"] == str(state_dir)


def test_state_dir_preserved(state_dir):
    marker = state_dir / "marker.txt"
    marker.write_text("preserve-me")
    runner = make_runner(returncode=0)
    result = updater.run_update(source="pypi", runner=runner)
    assert result["state_dir"] == str(state_dir)
    assert marker.read_text() == "preserve-me"


def test_run_update_pip_failure_fail_closed(state_dir):
    runner = make_runner(returncode=1, stderr="pip error: resolved nothing")
    result = updater.run_update(source="pypi", runner=runner)
    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["data"].startswith("UPDATE_FAILED")
    assert result["data_preserved"] is True
    assert str(state_dir) == result["state_dir"]


def test_run_update_unknown_source_fail_closed(state_dir):
    result = updater.run_update(source="floppy")
    assert result["ok"] is False
    assert result["status"] == "error"


def test_git_target_gets_git_prefix(state_dir):
    runner = make_runner(returncode=0)
    updater.run_update(source="git", runner=runner)
    cmd = runner.calls[0]["cmd"]
    assert any(spec.startswith("git+https://") for spec in cmd)


def test_mcp_sud_cli_update_disabled(state_dir, monkeypatch):
    from sud_cli import mcp_server
    assert "sud_cli_update" not in {t["name"] for t in mcp_server.tool_list()["tools"]}
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sud_cli_update", "arguments": {}}})
    # S3-4: инструмента нет в manifests — JSON-RPC -32602 (раньше: isError-текст)
    assert resp["error"]["code"] == -32602


def test_cli_update_check_json(tmp_path, monkeypatch, capsys):
    from sud_cli import cli
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("sud_cli.updater.check_update",
                        lambda source=None, target=None, runner=None: {
                            "current_version": "2.9.0", "latest_version": "2.9.0",
                            "update_available": False, "state_dir": str(tmp_path),
                            "data_safe": True})
    code = cli.main(["--json", "update", "--check", "--i-am-operator"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "ok"
    assert out["data"]["update_available"] is False


def test_update_rejects_arbitrary_target(state_dir):
    runner = make_runner(returncode=0)
    for target in ("git+https://evil.example/x", "file:///etc", "https://evil.example/x"):
        result = updater.run_update(source="git", target=target, runner=runner)
        assert result["ok"] is False
        assert result["status"] == "error"
        assert "target not allowed" in result["data"]
    assert runner.calls == [], "arbitrary target must be rejected before network"


def test_check_update_rejects_arbitrary_target(state_dir):
    runner = make_runner(returncode=0)
    result = updater.check_update(source="git", target="git+https://evil.example/x", runner=runner)
    assert result.get("ok") is False
    assert result["update_available"] is False
    assert "target not allowed" in result["data"]
    assert runner.calls == []


def test_cli_update_requires_operator(tmp_path, monkeypatch):
    from tests.conftest import run_cli
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    code, out, err = run_cli(["--json", "update"])
    assert code == 1
    payload = __import__("json").loads(err)
    assert payload["status"] == "error"
    assert payload["data"] == "обновление — только оператором, из доверенного источника"


def test_cli_update_rejects_arbitrary_target(tmp_path, monkeypatch):
    from tests.conftest import run_cli
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    code, out, err = run_cli(["--json", "update", "--i-am-operator",
                              "--target", "git+https://evil.example/x"])
    assert code == 1
    payload = __import__("json").loads(err)
    assert payload["status"] == "error"
    assert "target not allowed" in payload["data"]
