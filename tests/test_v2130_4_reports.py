"""the wave §4 (S-84): отчёты портфеля и дела.

Главный контракт — «не выдуманных метрик»: каждое число в конверте обязано
читаться из реальной таблицы, пустые секции выводятся []/{} (а не «нет
данных» и не догадка), суммы — Decimal, горизонт near/overdue переиспользует
определение окна из reminders/agenda, а не второе окно, заведённое рядом.
"""
import json
import re
from datetime import date, timedelta
from decimal import Decimal

from sud_cli.db import (
    add_case_amount,
    add_case_document,
    add_case_evidence,
    add_case_representative,
    create_case,
    get_connection,
    init_db,
    insert_deadline_state,
    set_case_amount,
)
from sud_cli.reports import REPORT_DISCLAIMER, case_payload, portfolio_payload
from tests.conftest import run_cli

AS_OF = date(2026, 11, 1)


def _seed_two_cases(state_dir):
    """Два дела: суммы по kind/status, представитель, near- и overdue-срок."""
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        a = create_case(conn, "A116-1/2026", "АС г. Москвы", "ООО Альфа", "ИП Бета",
                        status="pending", instance="apk")["case_id"]
        b = create_case(conn, "A116-2/2026", "АС СПб", "ООО Gamma", "ИП Delta",
                        status="closed", instance="apk")["case_id"]
        add_case_amount(conn, a, "fee", "15000.00")
        add_case_amount(conn, a, "fee", "5000")
        paid = add_case_amount(conn, b, "fee", "1000")
        add_case_amount(conn, b, "expert", "250.50")
        set_case_amount(conn, b, paid["id"], status="paid", paid_at="2026-10-01")
        add_case_representative(conn, a, side="plaintiff", name="Иванов И.И.")
        insert_deadline_state(conn, a, "as_appeal_decision",
                              due_at=(AS_OF + timedelta(days=10)).isoformat(),
                              state="computed")
        insert_deadline_state(conn, b, "as_cassation_district",
                              due_at=(AS_OF - timedelta(days=3)).isoformat(),
                              state="confirmed")
    finally:
        conn.close()
    return a, b


def _insert_state(state_dir, case_id, key, due_at, state="computed"):
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        insert_deadline_state(conn, case_id, key, due_at=due_at, state=state)
    finally:
        conn.close()


def test_portfolio_empty_is_zeroed_not_invented(tmp_path):
    init_db(tmp_path)
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    assert payload["cases"]["total"] == 0
    assert payload["cases"]["by_status"] == {
        "new": 0, "review": 0, "filed": 0, "pending": 0, "decided": 0,
        "appealed": 0, "closed": 0, "unknown": 0,
    }
    assert payload["deadlines"]["near"] == []
    assert payload["deadlines"]["overdue"] == []
    assert payload["problems"] == []
    assert payload["portfolio"] == []
    assert payload["amounts"]["by_kind"] == {}
    assert payload["amounts"]["totals"] == {"paid": "0", "planned": "0", "refunded": "0"}
    assert payload["events"]["total"] == 0


def test_portfolio_statuses_and_decimal_sums(tmp_path):
    _seed_two_cases(tmp_path)
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    assert payload["cases"]["total"] == 2
    assert payload["cases"]["by_status"]["pending"] == 1
    assert payload["cases"]["by_status"]["closed"] == 1
    # Decimal: 15000.00 + 5000 + 1000 (paid) = 21000.00 по kind, без хвоста
    assert payload["amounts"]["by_kind"]["fee"] == "21000.00"
    assert payload["amounts"]["by_kind"]["expert"] == "250.50"
    assert Decimal(payload["amounts"]["totals"]["planned"]) == Decimal("20250.50")
    assert Decimal(payload["amounts"]["totals"]["paid"]) == Decimal("1000")
    assert payload["amounts"]["totals"]["refunded"] == "0"
    assert isinstance(payload["amounts"]["totals"]["planned"], str)


def test_portfolio_near_and_overdue_use_one_window(tmp_path):
    """Окно near/overdue — одно и то же, что у agenda (W6a), а не своё."""
    a, b = _seed_two_cases(tmp_path)
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat(), days=30)
    near = payload["deadlines"]["near"]
    overdue = payload["deadlines"]["overdue"]
    assert [n["deadline_key"] for n in near] == ["as_appeal_decision"]
    assert near[0]["days_left"] == 10
    # закрытое дело из окна убирается ПРАВИЛОМ agenda — и это видно, а не молчок
    assert overdue == []
    dropped = payload["deadlines"]["closed_excluded"]
    assert [d["deadline_key"] for d in dropped] == ["as_cassation_district"]
    assert dropped[0]["case_id"] == b
    assert "agenda" in dropped[0]["reason"]
    assert payload["deadlines"]["near_count"] == len(near)
    assert payload["deadlines"]["closed_excluded_count"] == len(dropped)
    assert all(i["days_left"] >= 0 for i in near)


def test_portfolio_overdue_for_open_case(tmp_path):
    """Просрочка открытого дела остаётся в overdue — правило про закрытые, не про всё."""
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        cid = create_case(conn, "A116-9/2026", "АС г. Москвы", "ООО Альфа",
                          "ИП Бета", status="pending")["case_id"]
        insert_deadline_state(conn, cid, "as_appeal_decision",
                              due_at=(AS_OF - timedelta(days=2)).isoformat(),
                              state="computed")
    finally:
        conn.close()
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    overdue = payload["deadlines"]["overdue"]
    assert [o["deadline_key"] for o in overdue] == ["as_appeal_decision"]
    assert overdue[0]["days_left"] == -2
    assert payload["deadlines"]["closed_excluded"] == []


def test_portfolio_representatives_section(tmp_path):
    _seed_two_cases(tmp_path)
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    assert payload["representatives"]["total"] == 1
    assert payload["representatives"]["by_side"] == {"plaintiff": 1, "defendant": 0}


def test_portfolio_problems_are_counts_from_state_tables(tmp_path):
    """Без verify по файлам: problems — только счётчики строк state-таблиц."""
    a, _b = _seed_two_cases(tmp_path)
    _insert_state(tmp_path, a, "as_cure_left_without_movement", None)
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    problems = payload["problems"]
    assert len(problems) == payload["cases"]["total"]
    assert set(problems[0]) == {"case_id", "status", "events",
                                "deadline_states_without_due"}
    row_a = next(p for p in problems if p["case_id"] == a)
    assert row_a["deadline_states_without_due"] == 1
    assert row_a["status"] == "pending"


def test_portfolio_json_envelope_schema(tmp_path):
    _seed_two_cases(tmp_path)
    code, out, err = run_cli(["--json", "report", "portfolio", "--as-of",
                              AS_OF.isoformat()],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code == 0, err
    doc = json.loads(out)
    assert doc["status"] == "ok"
    assert doc["meta"]["command"] == "report portfolio"
    data = doc["data"]
    for key in ("as_of", "horizon_days", "cases", "events", "deadlines", "amounts",
                "representatives", "documents", "evidence", "problems", "portfolio",
                "disclaimer"):
        assert key in data, key
    assert data["disclaimer"] == REPORT_DISCLAIMER
    assert "Метрик успеха" in data["disclaimer"]


def test_portfolio_no_fabricated_success_words(tmp_path):
    """Ни «успешно», ни «рискованно», ни «рекомендуем»: только посчитанные строки.

    Disclaimer сканируется отдельно: он имеет право называть отсутствующие
    метрики своими именами («Метрик успеха… нет») — запрещены они в данных.
    """
    _seed_two_cases(tmp_path)
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    data = {k: v for k, v in payload.items() if k != "disclaimer"}
    text = json.dumps(data, ensure_ascii=False)
    for banned in ("успех", "эффективн", "качествен", "рекомендуем", "в целом",
                   "рискованн", "средн", "score", "rating", "kpi"):
        assert not re.search(banned, text, re.I), banned
    assert "Метрик успеха" in payload["disclaimer"]


def test_portfolio_human_mode_prints_sections(tmp_path):
    _seed_two_cases(tmp_path)
    code, out, err = run_cli(["report", "portfolio", "--as-of", AS_OF.isoformat()],
                            env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code == 0, err
    assert "Портфель на 2026-11-01" in out
    assert "дел: 2" in out
    assert "pending: 1" in out
    assert "расходы: paid=1000 planned=20250.50 refunded=0 RUB" in out
    assert REPORT_DISCLAIMER in out


def test_report_case_sections_and_matrix_summary(tmp_path):
    a, _b = _seed_two_cases(tmp_path)
    payload = case_payload(tmp_path, a)
    assert payload["case_id"] == a
    assert set(payload["sections"]) == {
        "case", "parties", "amounts", "documents", "documents_current", "notes",
        "events", "deadlines", "evidence", "representatives",
    }
    assert payload["sections"]["representatives"]["count"] == 1
    assert payload["matrix"]["instance"] == "apk"
    assert payload["matrix"]["points_total"] >= 2
    assert "as_appeal_decision" in payload["matrix"]["needs_anchor"] or \
        "as_appeal_decision" in payload["matrix"]["anchored"]
    assert payload["amounts_summary"]["totals_by_status"]["planned"] == "20000.00"


def test_report_case_marks_missing_evidence_file(tmp_path):
    """Evidence с file, которого больше нет: отчёт помечает MISSING_FILE, а не молчит."""
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        cid = create_case(conn, "A116-3/2026", "АС г. Москвы", "ООО Альфа",
                          "ИП Бета", status="pending")["case_id"]
        doc = tmp_path / "fotografiya.txt"
        doc.write_text("исходное", encoding="utf-8")
        add_case_evidence(conn, cid, title="Фотография помещения", path=str(doc))
    finally:
        conn.close()

    payload = case_payload(tmp_path, cid)
    assert payload["evidence_verify"]["intact"] == 1
    assert payload["evidence_problems"] == []

    doc.unlink()
    payload = case_payload(tmp_path, cid)
    assert payload["evidence_verify"]["missing_file"] == 1
    assert payload["evidence_verify"]["problems"] == 1
    assert [p["verdict"] for p in payload["evidence_problems"]] == ["MISSING_FILE"]
    assert payload["sections"]["documents"][0]["path"] == str(doc)


def test_report_case_missing_case_returns_error(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "report", "case", "case-absent"], env=env)
    assert code != 0
    assert "case not found" in (out + err)


def test_report_case_cli_json_and_human(tmp_path):
    a, _b = _seed_two_cases(tmp_path)
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "report", "case", a], env=env)
    assert code == 0, err
    doc = json.loads(out)
    assert doc["meta"]["command"] == "report case"
    assert doc["data"]["case_id"] == a

    code, out, err = run_cli(["report", "case", a], env=env)
    assert code == 0, err
    assert "A116-1/2026" in out
    assert "матрица: instance=apk" in out
    assert REPORT_DISCLAIMER in out


def test_report_portfolio_days_validation(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "report", "portfolio", "--days", "0"], env=env)
    assert code != 0
    assert "INVALID_ARGUMENTS" in (out + err)


def test_report_as_of_bad_date_errors(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "report", "portfolio", "--as-of", "не-дата"],
                             env=env)
    assert code != 0
    assert "INVALID_ARGUMENTS" in (out + err)


def test_report_documents_and_revisions_counts(tmp_path):
    """Документ-ревизия считается как ревизия: счётчики из case_documents."""
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        cid = create_case(conn, "A116-4/2026", "АС г. Москвы", "ООО Альфа",
                          "ИП Бета", status="new")["case_id"]
        doc = tmp_path / "isk.txt"
        doc.write_text("первая редакция", encoding="utf-8")
        add_case_document(conn, cid, str(doc))
        doc.write_text("вторая редакция", encoding="utf-8")
        add_case_document(conn, cid, str(doc))
    finally:
        conn.close()
    payload = portfolio_payload(tmp_path, as_of=AS_OF.isoformat())
    assert payload["documents"]["total"] == 2
    assert payload["documents"]["revisions"] == 1
    assert payload["cases"]["by_status"]["new"] == 1
