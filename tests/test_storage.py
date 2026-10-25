import json
from pathlib import Path

from sud_cli.config import get_state_dir
from tests.conftest import run_cli


def test_state_dir_isolated_via_env(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "providers"], env=env)
    assert code == 0
    data = json.loads(out)
    assert data["meta"]["state_dir"] == str(tmp_path)
    assert not (tmp_path / "store.db").exists()  # providers does not create store


def test_doctor_reports_healthy_after_init(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["healthy"] is True
    assert data["data"]["schema_version"] == "3"
    assert (tmp_path / "store.db").exists()


def test_config_get_state_dir_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    assert get_state_dir() == tmp_path


def test_config_default_is_under_home(monkeypatch):
    monkeypatch.delenv("SUD_CLI_STATE_DIR", raising=False)
    fake_home = Path("/nonexistent/fake-home")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert get_state_dir() == fake_home / ".local" / "share" / "sud-cli"
