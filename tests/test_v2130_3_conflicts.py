"""the wave §3 (S-27): конфликты расписания.

Набор event_type фиксирован ({слушание, заседание, предварительное},
регистр не важен) — угадывать по подстроке, что событие является заседанием,
движок не имеет права. Разные дела на одну дату = conflict; срок и слушание
одного и того же дела в один день = подсказка, а не конфликт.
"""
import json
from datetime import date, timedelta

from sud_cli.conflicts import (
    CONFLICTS_DISCLAIMER,
    HEARING_EVENT_TYPES,
    conflicts_payload,
    same_case_hints,
)
from sud_cli.db import anchor_deadline_state, create_case, get_connection, init_db
from tests.conftest import run_cli

DAY1 = date(2026, 11, 10)
DAY2 = date(2026, 11, 11)


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _case(tmp_path, conn, number, court="АС г. Москвы"):
    return create_case(conn, number, court, "ООО Альфа", "ИП Бета",
                       status="pending")["case_id"]


def _event(conn, event_id, case_id, day, event_type, doc_text="заседание"):
    conn.execute(
        "INSERT INTO events (event_id, case_id, event_date, event_type, doc_text,"
        " source_name, source_record_id, imported_at)"
        " VALUES (?, ?, ?, ?, ?, 'test', 'test', ?)",
        (event_id, case_id, day.isoformat() if day is not None else None,
         event_type, doc_text, day.isoformat() if day else "2026-01-01"),
    )
    conn.commit()


def _mk(tmp_path, number="A115-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "АС г. Москвы",
         "--plaintiff", "ООО Альфа", "--defendant", "ИП Бета", "--status", "pending"],
        env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _anchor(env, case_id, key, event_date, horizon_days=10):
    """Якорь так, чтобы due_at попал в окно: дата события = today + horizon - срок."""
    return run_cli(["--json", "deadlines", "anchor", case_id, "--key", key,
                    "--event-date", event_date, "--method", "вручение",
                    "--evidence", "уведомление", "--by", "lex"], env=env)


def test_two_cases_same_time_is_conflict(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-2/2026")
        b = _case(tmp_path, conn, "A115-3/2026")
        _event(conn, "ev-a", a, DAY1, "Слушание")
        _event(conn, "ev-b", b, DAY1, "заседание")
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    conflicts = payload["conflicts"]
    assert len(conflicts) == 1, conflicts
    item = conflicts[0]
    assert item["date"] == DAY1.isoformat()
    assert {item["case_a"], item["case_b"]} == {a, b}
    assert item["kinds"] == ["hearing"]
    assert payload["same_case_hints"] == []


def test_same_case_deadline_plus_hearing_is_hint_not_conflict(tmp_path):
    env, case_id = _mk(tmp_path, "A115-4/2026")
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        _event(conn, "ev-hint", case_id, DAY1, "заседание")
        insert = anchor_deadline_state(conn, tmp_path, case_id, "soyu_appeal_decision",
                                       DAY1.isoformat(), "вручение", "уведомление")
        due = insert["due_at"]
    finally:
        conn.close()
    assert due
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    by_date = {}
    for e in payload["entries"]:
        by_date.setdefault(e["on_date"], set()).add(e["case_id"])
    # в день слушания — одно дело: конфликт невозможен по определению
    if DAY1.isoformat() in by_date:
        assert len(by_date[DAY1.isoformat()]) == 1
    assert payload["conflicts"] == []

    # искусно соберём подсказку: срок того же дела ровно на дату слушания
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        conn.execute(
            "INSERT INTO deadline_states (case_id, deadline_key, due_at, state, created_at)"
            " VALUES (?, 'as_appeal_decision', ?, 'computed', '2026-01-01T00:00:00+00:00')",
            (case_id, DAY1.isoformat()))
        conn.commit()
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    assert payload["conflicts"] == [], "тот же дело+срок — не конфликт разных дел"
    hints = payload["same_case_hints"]
    assert any(h["case_id"] == case_id and h["date"] == DAY1.isoformat() for h in hints), hints
    hint = next(h for h in hints if h["case_id"] == case_id)
    assert hint["hearings"] == ["ev-hint"]
    assert hint["deadlines"] == ["as_appeal_decision"]


def test_different_days_no_conflict(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-5/2026")
        b = _case(tmp_path, conn, "A115-6/2026")
        _event(conn, "ev-x", a, DAY1, "слушание")
        _event(conn, "ev-y", b, DAY2, "слушание")
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    assert payload["conflicts"] == []
    assert payload["same_case_hints"] == []


def test_days_window_excludes_later_events(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-7/2026")
        b = _case(tmp_path, conn, "A115-8/2026")
        far = DAY1 + timedelta(days=40)
        _event(conn, "ev-far-a", a, far, "слушание")
        _event(conn, "ev-far-b", b, far, "слушание")
    finally:
        conn.close()
    inside = conflicts_payload(tmp_path, days=60, today=DAY1)
    assert len(inside["conflicts"]) == 1
    outside = conflicts_payload(tmp_path, days=10, today=DAY1)
    assert outside["conflicts"] == []
    assert outside["entries"] == []
    assert outside["empty"] is True


def test_event_type_outside_fixed_set_is_ignored(tmp_path):
    """'публикавание решения' не слушание: подстроку не матчим, тип не угадываем."""
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-9/2026")
        b = _case(tmp_path, conn, "A115-10/2026")
        _event(conn, "ev-p1", a, DAY1, "публикация решения")
        _event(conn, "ev-p2", b, DAY1, "публикация решения")
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    assert payload["conflicts"] == []
    assert payload["entries"] == []
    assert payload["hearing_event_types"] == sorted(HEARING_EVENT_TYPES)


def test_event_type_matching_is_case_insensitive(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-11/2026")
        b = _case(tmp_path, conn, "A115-12/2026")
        _event(conn, "ev-up-a", a, DAY1, "СЛУШАНИЕ")
        _event(conn, "ev-up-b", b, DAY1, "  Предварительное  ")
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    assert len(payload["conflicts"]) == 1


def test_invalid_dates_are_skipped_not_fatal(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-13/2026")
        b = _case(tmp_path, conn, "A115-14/2026")
        _event(conn, "ev-bad", a, None, "слушание")
        conn.execute(
            "INSERT INTO events (event_id, case_id, event_date, event_type, doc_text,"
            " source_name, source_record_id, imported_at)"
            " VALUES ('ev-bad-2', ?, 'не-дата', 'заседание', 'х', 't', 't', '2026-01-01')",
            (a,))
        _event(conn, "ev-ok-a", a, DAY1, "слушание")
        _event(conn, "ev-ok-b", b, DAY1, "слушание")
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    assert len(payload["skipped"]) == 2, payload["skipped"]
    assert {s["ref"] for s in payload["skipped"]} == {"ev-bad", "ev-bad-2"}
    assert len(payload["conflicts"]) == 1, "битые даты не утаскивают за собой весь детект"


def test_deadline_cross_case_on_hearing_date_is_conflict(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-15/2026")
        b = _case(tmp_path, conn, "A115-16/2026")
        _event(conn, "ev-a15", a, DAY1, "заседание")
        conn.execute(
            "INSERT INTO deadline_states (case_id, deadline_key, due_at, state, created_at)"
            " VALUES (?, 'as_cassation_district', ?, 'confirmed', '2026-01-01T00:00:00+00:00')",
            (b, DAY1.isoformat()))
        conn.commit()
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    conflicts = payload["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["kinds"] == ["deadline", "hearing"]
    assert {e["kind"] for e in conflicts[0]["entries"]} == {"deadline", "hearing"}


def test_waived_and_null_due_states_do_not_participate(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        a = _case(tmp_path, conn, "A115-17/2026")
        b = _case(tmp_path, conn, "A115-18/2026")
        _event(conn, "ev-a17", a, DAY1, "слушание")
        for state in ("waived", "needs_confirm"):
            conn.execute(
                "INSERT INTO deadline_states (case_id, deadline_key, due_at, state, created_at)"
                " VALUES (?, 'as_appeal_decision', ?, ?, '2026-01-01T00:00:00+00:00')",
                (b, DAY1.isoformat(), state))
        conn.execute(
            "INSERT INTO deadline_states (case_id, deadline_key, due_at, state, created_at)"
            " VALUES (?, 'kas_appeal_decision', NULL, 'computed', '2026-01-01T00:00:00+00:00')",
            (b,))
        conn.commit()
    finally:
        conn.close()
    payload = conflicts_payload(tmp_path, days=4000, today=DAY1)
    assert payload["conflicts"] == []
    assert [e["case_id"] for e in payload["entries"]] == [a]


def test_conflicts_cli_envelope_and_disclaimer(tmp_path):
    env, case_id = _mk(tmp_path, "A115-19/2026")
    code, out, err = run_cli(["--json", "calendar", "conflicts", "--days", "30"], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["conflicts"] == []
    assert data["same_case_hints"] == []
    assert data["skipped"] == []
    assert data["disclaimer"] == CONFLICTS_DISCLAIMER
    assert "справ" in data["disclaimer"].lower()


def test_conflicts_days_validation(tmp_path):
    env, _ = _mk(tmp_path, "A115-20/2026")
    code, out, err = run_cli(["--json", "calendar", "conflicts", "--days", "0"], env=env)
    assert code != 0
    assert "INVALID_ARGUMENTS" in (out + err)


def test_same_case_hints_helper_is_pure(tmp_path):
    entries = [
        {"kind": "hearing", "case_id": "c1", "ref": "e1", "on_date": "2026-11-10"},
        {"kind": "deadline", "case_id": "c1", "ref": "as_appeal_decision",
         "on_date": "2026-11-10"},
        {"kind": "deadline", "case_id": "c2", "ref": "kas_cassation",
         "on_date": "2026-11-11"},
    ]
    hints = same_case_hints(entries)
    assert len(hints) == 1
    assert hints[0]["case_id"] == "c1"
    assert hints[0]["hearings"] == ["e1"]
