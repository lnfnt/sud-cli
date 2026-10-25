"""W6a §4 (S-60): алерты case-search → bus-free a2a-строки (sud шину не зовёт).

Эталон — the wave (reminders): строки только печатаются, subprocess в модуле и в
хендлере запрещён и проверяется grep-гейтом по исходнику. Пусто — это [], а
не падение; --days валидируется; в doctor — счётчик alerts_pending.
"""
import inspect
import json
import shlex
from datetime import date, datetime, timedelta, timezone

from sud_cli import court_alerts
from tests.conftest import run_cli


def _mk_alert(alerts_dir, case="А40-1/2026", status="changed",
              created=None, changes=None):
    alerts_dir.mkdir(parents=True, exist_ok=True)
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "case_number": case,
        "status": status,
        "reason": "snapshot diff detected" if status == "changed" else status,
        "changes": changes if changes is not None else [
            {"path": " HearingInfo.0.date", "before": None, "after": "2026-11-01"}],
        "destination": "local-file",
        "created_at": created,
    }
    safe = case.replace("/", "_")
    (alerts_dir / f"{safe}@{created}-{status}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def test_alerts_fixture_produces_shlex_roundtrip_row(tmp_path):
    env = _env(tmp_path)
    adir = tmp_path / "alerts"
    _mk_alert(adir)
    code, out, err = run_cli(["--json", "case-search", "alerts", "--days", "7",
                              "--alerts-dir", str(adir)], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert len(data["commands"]) == 1
    cmd = data["commands"][0]
    assert "А40-1/2026" in cmd
    # shlex roundtrip: строка парсится обратно в аргументы
    argv = shlex.split(cmd)
    assert argv[:3] == ["a2a", "mail", "send"]
    assert "--priority" in argv and argv[argv.index("--priority") + 1] == "today"
    assert "--subject" in argv and argv[argv.index("--subject") + 1]
    assert "--verify" in argv
    assert "case-search alerts --days 7 --json" in argv[argv.index("--verify") + 1]
    assert "--verify-run" in argv
    body = argv[argv.index("--body") + 1]
    assert "изменений: 1" in body


def test_alerts_subject_carries_case_and_status(tmp_path):
    env = _env(tmp_path)
    adir = tmp_path / "alerts"
    _mk_alert(adir, case="А40-2/2026", status="error")
    code, out, err = run_cli(["--json", "case-search", "alerts", "--alerts-dir",
                              str(adir)], env=env)
    assert code == 0, err
    cmd = json.loads(out)["data"]["commands"][0]
    argv = shlex.split(cmd)
    subject = argv[argv.index("--subject") + 1]
    assert "А40-2/2026" in subject and "error" in subject


def test_alerts_empty_directory_is_empty_list(tmp_path):
    env = _env(tmp_path)
    adir = tmp_path / "alerts"
    adir.mkdir()
    code, out, err = run_cli(["--json", "case-search", "alerts", "--alerts-dir",
                              str(adir)], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["alerts"] == [] and data["commands"] == []


def test_alerts_days_window_filters_old(tmp_path):
    env = _env(tmp_path)
    adir = tmp_path / "alerts"
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _mk_alert(adir, case="А40-3/2026", created=old)
    _mk_alert(adir, case="А40-4/2026")
    code, out, err = run_cli(["--json", "case-search", "alerts", "--days", "7",
                              "--alerts-dir", str(adir)], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    cases = [a["case_number"] for a in data["alerts"]]
    assert "А40-4/2026" in cases
    assert "А40-3/2026" not in cases
    assert len(data["commands"]) == len(data["alerts"])


def test_alerts_days_validation(tmp_path):
    env = _env(tmp_path)
    for bad in ("0", "91", "-3"):
        code, out, err = run_cli(["--json", "case-search", "alerts", "--days", bad], env=env)
        assert code != 0 or "INVALID_ARGUMENTS" in (out + err), (bad, out, err)


def test_alerts_source_is_bus_free():
    """grep-гейт: ни модуль, ни cmd-хендлер не умеют запускать процессы."""
    src = inspect.getsource(court_alerts)
    cmd_src = inspect.getsource(
        __import__("sud_cli.cli", fromlist=["cmd_case_search_alerts"])
        .cmd_case_search_alerts)
    for needle in ("subprocess", "os.system", "popen", "Popen"):
        assert needle not in src, needle
        assert needle not in cmd_src, needle


def test_doctor_reports_alerts_pending(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)  # isolate cwd-level .case-search candidates
    state_dir = tmp_path
    # состояние doctor ищёт алерты в state-dir/case-search-alerts
    adir = state_dir / "case-search-alerts"
    _mk_alert(adir, case="А40-5/2026")
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["alerts_pending"] == 1
