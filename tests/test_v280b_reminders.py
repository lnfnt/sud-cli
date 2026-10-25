"""the wave §2: reminders due — bus-free генератор готовых a2a-строк (sud шину не зовёт)."""
import inspect
import json
import shlex
from datetime import date, timedelta

from sud_cli.legal_deadlines import calculate_deadline
from sud_cli.legal_engine import load_rules
from tests.conftest import run_cli


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk_case(tmp_path, number):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "AC",
         "--plaintiff", "P", "--defendant", "D"], env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _anchor(env, case_id, key, event_date):
    code, out, err = run_cli(
        ["--json", "deadlines", "anchor", case_id, "--key", key,
         "--event-date", event_date, "--method", "вручение",
         "--evidence", "w4b-test"], env=env)
    assert code == 0, err
    return _j(out)["data"]


def _expected_due(key, event):
    res = calculate_deadline(deadline_key=key, event_date=event, rules=load_rules())
    assert getattr(res, "status", None) == "ok", getattr(res, "detail", None)
    return res.deadline


def test_reminders_due_empty_window(tmp_path):
    env, _ = _mk_case(tmp_path, "R111-1/2026")
    code, out, err = run_cli(["--json", "reminders", "due", "--days", "7"], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["items"] == []
    assert data["commands"] == []


def test_reminders_due_in_window_anchored(tmp_path):
    env, case_id = _mk_case(tmp_path, "R111-2/2026")
    ev = date.today()
    due = _expected_due("as_appeal_decision", ev)  # арифметика — тем же движком
    _anchor(env, case_id, "as_appeal_decision", ev.isoformat())
    days = (due - ev).days + 5
    assert 1 <= days <= 90
    code, out, err = run_cli(["--json", "reminders", "due", "--days", str(days)], env=env)
    assert code == 0, err
    items = _j(out)["data"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["case_id"] == case_id
    assert item["deadline_key"] == "as_appeal_decision"
    assert item["due_at"] == due.isoformat()
    assert item["days_left"] == (due - date.today()).days
    assert item["days_left"] >= 0


def test_reminders_due_excludes_overdue_waived_and_null(tmp_path):
    env, case_id = _mk_case(tmp_path, "R111-3/2026")
    # overdue: due уже прошёл
    ev_old = date.today() - timedelta(days=15)
    _anchor(env, case_id, "soyu_court_order_cancel", ev_old.isoformat())
    # waived: активный срок, но снят waiver-нормой
    ev = date.today()
    _anchor(env, case_id, "as_appeal_decision", ev.isoformat())
    code, out, err = run_cli(
        ["--json", "deadlines", "waive", case_id, "--key", "as_appeal_decision",
         "--by", "lex", "--norm", "тест-waiver"], env=env)
    assert code == 0, err
    # null due_at: confirm без предшествующего anchor -> due_at NULL, пропускается
    code, out, err = run_cli(
        ["--json", "deadlines", "confirm", case_id, "--key", "kas_appeal_decision",
         "--by", "lex"], env=env)
    assert code == 0, err
    code, out, err = run_cli(["--json", "reminders", "due", "--days", "30"], env=env)
    assert code == 0, err
    items = _j(out)["data"]["items"]
    keys = [i["deadline_key"] for i in items]
    assert keys == []


def test_generated_command_contains_verify(tmp_path):
    env, case_id = _mk_case(tmp_path, "R111-4/2026")
    ev = date.today()
    due = _expected_due("soyu_court_order_cancel", ev)
    _anchor(env, case_id, "soyu_court_order_cancel", ev.isoformat())
    days = 30
    code, out, err = run_cli(["--json", "reminders", "due", "--days", str(days)], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert len(data["commands"]) == 1
    cmd = data["commands"][0]
    assert cmd.startswith("a2a mail send")
    assert "--verify" in cmd and "--verify-run" in cmd
    toks = shlex.split(cmd)
    assert toks[toks.index("--to") + 1] == "colleague-legal"
    assert toks[toks.index("--priority") + 1] == "today"
    assert toks[toks.index("--subject") + 1] == f"deadline: {case_id}/soyu_court_order_cancel до {due.isoformat()}"
    assert toks[toks.index("--verify") + 1] == f"/usr/bin/sud-cli reminders due --days {days} --json"
    body = toks[toks.index("--body") + 1]
    assert any(ch.isdigit() for ch in body)  # числа есть => verify обязателен по политике a2a


def test_reminders_source_is_bus_free():
    import sud_cli.cli
    import sud_cli.reminders as rem
    src_rem = inspect.getsource(rem)
    src_cmd = inspect.getsource(sud_cli.cli.cmd_reminders_due)
    for src in (src_rem, src_cmd):
        assert "subprocess" not in src
        assert "os.system" not in src
        assert "Popen" not in src
    # шина только печатается как текст, не вызывается
    assert "bus-free" in src_rem


def test_reminders_days_validation(tmp_path):
    env, _ = _mk_case(tmp_path, "R111-5/2026")
    for bad in ("0", "91", "-3"):
        code, out, err = run_cli(["--json", "reminders", "due", "--days", bad], env=env)
        assert code != 0, bad
        data = _j(out if out.strip() else err)
        assert "INVALID_ARGUMENTS" in json.dumps(data)


def test_doctor_reports_reminders_available(tmp_path):
    env, _ = _mk_case(tmp_path, "R111-6/2026")
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0, err
    assert _j(out)["data"]["reminders_available"] == 0
    # подобрать событие так, чтобы due попал в окно 7 дней — арифметика движка, не хардкод
    key = "soyu_court_order_cancel"
    picked = None
    for back in range(0, 12):
        ev = date.today() - timedelta(days=back)
        due = _expected_due(key, ev)
        if date.today() <= due <= date.today() + timedelta(days=7):
            picked = ev
            break
    assert picked is not None
    _, case_id = _mk_case(tmp_path, "R111-7/2026")
    _anchor(env, case_id, key, picked.isoformat())
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert _j(out)["data"]["reminders_available"] >= 1


def test_quoting_escapes_single_quote():
    from sud_cli.reminders import generate_mail_commands
    items = [{"case_id": "d'oe'uvre-1", "deadline_key": "as_appeal_decision",
              "due_at": "2026-11-01", "days_left": 7, "state": "computed"}]
    cmd = generate_mail_commands(items, days=7)[0]
    toks = shlex.split(cmd)
    assert toks[toks.index("--subject") + 1] == "deadline: d'oe'uvre-1/as_appeal_decision до 2026-11-01"
    assert "\"'\"" in cmd  # shlex-экранирование одинарной кавычки: '"'"'
