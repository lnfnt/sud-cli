"""W6a §5 (S-23): автозаполнение точек срока из текстов актов.

Главная ловушка волны: в утверждённой deadline_matrix cure-срока нет,
поэтому маркер «оставлено без движения» НЕ имеет права рождать выдуманную
точку — честный вывод это manual_review-hint. Всё создаваемое — computed +
needs_confirm; confirmed autofill не делает никогда; повтор идемпотентен;
словарь маркеров ссылается только на существующие 15 ключей матрицы и не
содержит нормативных текстов.
"""
import json
from datetime import date

from sud_cli import db
from sud_cli.autofill import MARKERS_PATH, autofill_case, load_markers
from sud_cli.deadline_matrix import INSTANCE_TO_JURISDICTION
from sud_cli.legal_deadlines import calculate_deadline
from sud_cli.legal_engine import STATUS_OK, load_rules
from tests.conftest import run_cli


def _mk_case(tmp_path, number="S23-1/2026"):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", number,
                              "--court", "АС г. Москвы", "--plaintiff", "П",
                              "--defendant", "Д"], env=env)
    assert code == 0, err
    return env, json.loads(out)["data"]["case_id"]


def _add_event(tmp_path, case_id, event_id, event_date, doc_text,
               event_type="определение"):
    conn = db.get_connection(tmp_path)
    conn.execute(
        "INSERT INTO events (event_id, case_id, event_date, event_type, doc_text,"
        " source_name, source_record_id, imported_at) VALUES (?,?,?,?,?,?,?,?)",
        (event_id, case_id, event_date, event_type, doc_text, "fixture", event_id,
         "2026-10-24T00:00:00Z"))
    conn.commit()
    conn.close()


def _states(tmp_path, case_id):
    conn = db.get_connection(tmp_path)
    try:
        return db.list_deadline_states(conn, case_id)
    finally:
        conn.close()


def test_left_without_movement_creates_cure_point_without_inventing_date(tmp_path):
    """Обновлено 24.10: юридическая проверка дал cure-строку (duration=null). Точка
    создаётся, но БЕЗ выдуманной даты: due_at=None + needs_confirm;
    как_appeal_decision по-прежнему не рождается."""
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-1", "2026-09-01",
               "Резолютивная часть определения оставлена без движения")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    created = [c for c in data["created"] if c.get("deadline_key") == "as_cure_left_without_movement"]
    assert created, data
    row = created[0]
    assert row["due_at"] is None, "выдуманная дата cure-срока запрещена"
    assert row["basis"]["needs_confirm"] is True
    assert data["manual_review_hints"] == []
    states = _states(tmp_path, case_id)
    assert all(s["deadline_key"] != "as_appeal_decision" for s in states)


def test_final_form_marker_creates_computed_needs_confirm(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-2", "2026-09-01",
               "Определение изготовлено в окончательной форме")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["created"], data
    keys = {r["deadline_key"] for r in data["created"]}
    assert "as_appeal_decision" in keys
    rules = load_rules()
    for row in data["created"]:
        expected = calculate_deadline(deadline_key=row["deadline_key"],
                                      event_date=date(2026, 9, 1), rules=rules)
        assert expected.status == STATUS_OK
        assert row["state"] == "computed"
        assert row["confirmed_by"] is None
        assert row["needs_confirm"] is True
        assert row["due_at"] == expected.deadline.isoformat()
        basis = row["basis"]
        assert basis["marker_id"] == "autofill:event:ev-2:marker:final_form_date"
        assert basis["needs_confirm"] is True
        assert basis["event_date"] == "2026-09-01"
        assert basis["method"] == "autofill"
    # никогда confirmed: перечитываем из БД
    stored = _states(tmp_path, case_id)
    assert all(s["state"] == "computed" for s in stored)


def test_received_marker_maps_to_court_order_cancellation(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-3", "2026-10-05",
               "Копия судебного приказа получена")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    assert code == 0, err
    keys = {r["deadline_key"] for r in json.loads(out)["data"]["created"]}
    assert {"as_court_order_cancel", "soyu_court_order_cancel",
            "kas_court_order_cancel"} <= keys


def test_unknown_text_skips_honestly(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-4", "2026-09-01",
               "Определение о передаче дела по подсудности")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["created"] == []
    assert {"event_id": "ev-4", "reason": "no_markers"} in data["skipped"]
    assert _states(tmp_path, case_id) == []


def test_two_events_processed(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-a", "2026-08-03",
               "Определение изготовлено в окончательной форме")
    _add_event(tmp_path, case_id, "ev-b", "2026-09-14",
               "Копия определения получена")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    data = json.loads(out)["data"]
    assert data["events_scanned"] == 2
    mids = {r["basis"]["marker_id"] for r in data["created"]}
    assert any("ev-a" in m for m in mids) and any("ev-b" in m for m in mids)


def test_repeat_idempotent_no_duplicates(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-c", "2026-08-03",
               "Определение изготовлено в окончательной форме")
    run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    before = len(_states(tmp_path, case_id))
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["created"] == []
    assert any(s.get("reason") == "already_autofilled" for s in data["skipped"]), data["skipped"]
    assert len(_states(tmp_path, case_id)) == before


def test_event_filter_limits_scope(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-d", "2026-08-03",
               "Определение изготовлено в окончательной форме")
    _add_event(tmp_path, case_id, "ev-e", "2026-09-14",
               "Копия судебного приказа получена")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id,
                              "--event", "ev-d"], env=env)
    data = json.loads(out)["data"]
    assert data["events_scanned"] == 1
    mids = {r["basis"]["marker_id"] for r in data["created"]}
    assert mids and all("ev-d" in m for m in mids)


def test_unparsable_event_date_skipped(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-f", "",
               "Определение изготовлено в окончательной форме")
    code, out, err = run_cli(["--json", "deadlines", "autofill", case_id], env=env)
    data = json.loads(out)["data"]
    assert data["created"] == []
    assert {"event_id": "ev-f", "reason": "event_date_unparsable",
            "markers": ["final_form_date"]} in data["skipped"]


def test_dictionary_only_references_existing_matrix_keys_and_no_norms():
    raw_text = MARKERS_PATH.read_text(encoding="utf-8")
    dictionary = json.loads(raw_text)
    matrix = load_rules().get("deadline_matrix", {})
    assert len(matrix) == 16, "16 VERIFIED ключей: +as_cure_left_without_movement (юридическая проверка 24.10)"
    for marker in dictionary["markers"]:
        assert marker["cure"] or marker["deadline_keys"], marker
        for key in marker["deadline_keys"]:
            assert key in matrix, f"новый key матрицы запрещён: {key}"
        assert set(marker) <= {"name", "patterns", "deadline_keys", "cure"}
    # нормативных текстов в словаре нет: ссылки «ст.» не появляются
    assert "ст." not in raw_text
    assert "ст " not in raw_text


def test_created_keys_are_matrix_jurisdictions(tmp_path):
    env, case_id = _mk_case(tmp_path)
    _add_event(tmp_path, case_id, "ev-g", "2026-07-01",
               "Определение изготовлено в окончательной форме")
    conn = db.get_connection(tmp_path)
    try:
        data = autofill_case(conn, tmp_path, case_id)
    finally:
        conn.close()
    matrix = load_rules()["deadline_matrix"]
    for row in data["created"]:
        assert row["deadline_key"] in matrix
