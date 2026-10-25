import json
from pathlib import Path

from sud_cli.db import SCHEMA_VERSION, get_connection, init_db
from tests.conftest import run_cli


def env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def J(out):
    return json.loads(out)


def make_case(tmp_path):
    e = env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-1/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=e)
    assert code == 0, err
    return e, J(out)["data"]["case_id"]


def test_deadline_calculate_writes_computed(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    assert code == 0, err
    data = J(out)["data"]
    assert "warning_basis" in data and data["warning_basis"] == "event_date_assumed"
    # db state
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    rows = conn.execute("SELECT * FROM deadline_states WHERE case_id=?", (cid,)).fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["state"] == "computed"
    conn.close()


def test_anchor_supersedes_and_basis(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "заказное 1", "--by", "lex"], env=e)
    assert code == 0, err
    d = J(out)["data"]
    assert d["basis"]["method"] == "вручение"
    assert d["basis"]["evidence"] == "заказное 1"
    # second anchor
    code, out, err = run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-10", "--method", "почта", "--evidence", "2"], env=e)
    assert code == 0
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    rows = conn.execute("SELECT * FROM deadline_states WHERE case_id=? AND deadline_key=? ORDER BY id", (cid, "as_appeal_decision")).fetchall()
    assert len(rows) == 2
    conn.close()


def test_confirm_and_waive_audit(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "confirm", cid, "--key", "as_appeal_decision", "--by", "lex", "--evidence", "вручение 12.10"], env=e)
    assert code == 0, err
    d = J(out)["data"]
    assert d["state"] == "confirmed"
    assert d["confirmed_by"] == "lex"
    assert "вручение" in (d.get("evidence_note") or "")
    # waive separate key
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_ruling", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "waive", cid, "--key", "as_appeal_ruling", "--by", "lex", "--norm", "ст. 114 АПК РФ", "--evidence", "x"], env=e)
    assert code == 0, err
    d = J(out)["data"]
    assert d["state"] == "waived"
    assert d["evidence_note"] and "ст. 114" in d["evidence_note"]
    # audit
    code, out, err = run_cli(["--json", "deadlines", "audit", cid], env=e)
    assert code == 0
    entries = J(out)["data"]
    assert len(entries) >= 2
    assert all(set(e).issuperset({"ts", "actor", "case", "key", "action", "before", "after"}) for e in entries)


def test_confirm_flag_on_calculate(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09", "--confirm", "--by", "calc"], env=e)
    assert code == 0, err
    data = J(out)["data"]
    assert data["state"] == "confirmed"
    assert data["confirmed_by"] == "calc"


def test_case_card_shows_state(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "case", "card", cid], env=e)
    assert code == 0
    dl = J(out)["data"]["deadlines"]
    assert dl and dl[0]["state"] == "computed"
    assert dl[0]["confirmed_by"] is None


def test_deadlines_list(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "list", cid], env=e)
    assert code == 0
    rows = J(out)["data"]
    assert rows and rows[0]["state"] == "computed"


def test_calendar_agenda(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-01-01"], env=e)
    code, out, err = run_cli(["--json", "calendar", "agenda", "--days", "14"], env=e)
    assert code == 0, err
    data = J(out)["data"]
    assert "overdue" in data and "next_5" in data
    assert len(data["overdue"]) >= 1
    # empty lists explicit
    tmp2 = tmp_path / "empty"
    tmp2.mkdir()
    e2 = {"SUD_CLI_STATE_DIR": str(tmp2)}
    code, out, err = run_cli(["--json", "calendar", "agenda"], env=e2)
    assert code == 0
    d2 = J(out)["data"]
    assert d2["empty"] is True
    assert d2["overdue"] == []
    assert d2["next_5"] == []


def test_doctor_freshness(tmp_path):
    e = env(tmp_path)
    code, out, err = run_cli(["--json", "doctor"], env=e)
    assert code == 0
    data = J(out)["data"]
    assert "freshness" in data
    f = data["freshness"]
    assert "key_rates" in f and "holidays" in f and "rules" in f
    assert "warnings" in f


def test_waiver_requires_norm(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "waive", cid, "--key", "as_appeal_decision", "--by", "lex"], env=e)
    # missing norm -> argparse error or INVALID_ARGUMENTS
    assert code != 0


def test_confirm_waive_flag(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "confirm", cid, "--key", "as_appeal_decision", "--by", "lex", "--waive", "--norm", "ст. 115 АПК"], env=e)
    assert code == 0, err
    assert J(out)["data"]["state"] == "waived"


def test_calculate_after_anchor_no_warning(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "заказное 1"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    assert code == 0, err
    data = J(out)["data"]
    assert "warning_basis" not in data
    assert data["state"] == "computed"


def test_case_card_confirmed_by_after_confirm(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    run_cli(["--json", "deadlines", "confirm", cid, "--key", "as_appeal_decision", "--by", "lex", "--evidence", "вручение 12.10"], env=e)
    code, out, err = run_cli(["--json", "case", "card", cid], env=e)
    assert code == 0
    dl = J(out)["data"]["deadlines"]
    assert dl and dl[0]["state"] == "confirmed"
    assert dl[0]["confirmed_by"] == "lex"


def test_waive_updates_state(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "waive", cid, "--key", "as_appeal_decision", "--by", "lex", "--norm", "ст. 115 АПК"], env=e)
    assert code == 0, err
    code, out, err = run_cli(["--json", "deadlines", "list", cid], env=e)
    rows = J(out)["data"]
    assert rows and rows[-1]["state"] == "waived"


def test_calendar_agenda_next_5_limit(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-01-01"], env=e)
    code, out, err = run_cli(["--json", "calendar", "agenda", "--days", "365"], env=e)
    assert code == 0
    data = J(out)["data"]
    assert len(data["next_5"]) <= 5
    assert isinstance(data["overdue"], list)


def test_anchor_basis_contains_fields(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "почта", "--evidence", "письмо 2", "--by", "lex"], env=e)
    assert code == 0, err
    basis = J(out)["data"]["basis"]
    for k in ("event_date", "method", "evidence", "actor", "ts"):
        assert k in basis


def test_audit_entry_keys(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    run_cli(["--json", "deadlines", "confirm", cid, "--key", "as_appeal_decision", "--by", "lex"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "audit", cid], env=e)
    assert code == 0
    entries = J(out)["data"]
    assert entries
    for e_ in entries:
        assert {"ts", "actor", "case", "key", "action", "before", "after"}.issubset(e_.keys())


def test_anchor_supersede_rows(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "1"], env=e)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-10", "--method", "почта", "--evidence", "2"], env=e)
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    rows = conn.execute("SELECT * FROM deadline_states WHERE case_id=? AND deadline_key=?", (cid, "as_appeal_decision")).fetchall()
    assert len(rows) == 2
    conn.close()


def test_confirm_flag_on_calculate_state(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09", "--confirm", "--by", "calc"], env=e)
    assert code == 0, err
    data = J(out)["data"]
    assert data["state"] == "confirmed"
    assert data["confirmed_by"] == "calc"


def test_deadlines_list_state(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "list", cid], env=e)
    assert code == 0
    rows = J(out)["data"]
    assert rows and rows[0]["state"] == "computed"


def test_doctor_freshness_structure(tmp_path):
    e = env(tmp_path)
    code, out, err = run_cli(["--json", "doctor"], env=e)
    assert code == 0
    f = J(out)["data"]["freshness"]
    assert "key_rates" in f and "holidays" in f and "rules" in f
    assert "warnings" in f


def test_waiver_requires_norm_error(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "waive", cid, "--key", "as_appeal_decision", "--by", "lex"], env=e)
    assert code != 0


def test_anchor_unknown_key_fails_loud(tmp_path):
    """правило «тексты только из реестра»: ключ вне deadline_matrix = отказ, не молчаливый computed/None (контроль 24.10)."""
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "anchor", cid, "--key", "appeal_as",
                              "--event-date", "2026-10-20", "--method", "иное", "--evidence", "x"], env=e)
    assert code == 1
    assert "invalid_deadline_key" in (out + err)


def test_anchor_evidence_visible_in_row(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision",
                              "--event-date", "2026-10-20", "--method", "почта",
                              "--evidence", "заказное-156"], env=e)
    assert code == 0, err
    assert "заказное-156" in out
