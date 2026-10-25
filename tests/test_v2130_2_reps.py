"""the wave §2 (S-15): представители по делу.

Контракт: таблица case_representatives по ensure-паттерну the wave/the wave
(SCHEMA_VERSION не bump'ится), add/list roundtrip, секция в card, пустой
список — явно [], side валидируется, и в каждом выводе — hint, что
полномочия проверяются вручную: sud-cli не реестр доверенностей и
автоматической проверки не делает.
"""
import json
import sqlite3

from sud_cli.db import (
    REPRESENTATIVE_HINT,
    SCHEMA_VERSION,
    add_case_representative,
    get_connection,
    init_db,
)
from tests.conftest import run_cli

CASE_COLUMNS_V3 = [
    "case_id", "case_number", "court", "plaintiff", "defendant", "source_name",
    "source_record_id", "fetched_at", "source_url", "imported_at", "status",
    "owner", "closed_at",
]


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk(tmp_path, number="A114-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "АС г. Москвы",
         "--plaintiff", "ООО Альфа", "--defendant", "ИП Бета", "--status", "pending"],
        env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _add(env, case_id, side="plaintiff", name="Иванов И.И.", extra=()):
    return run_cli(["--json", "case", "rep", "add", case_id, "--side", side,
                    "--name", name, *extra], env=env)


def test_rep_add_list_roundtrip(tmp_path):
    env, case_id = _mk(tmp_path, "A114-2/2026")
    code, out, err = _add(env, case_id, extra=[
        "--authority-note", "по доверенности от 01.09.2026",
        "--power-docs", "дов. №1-д, срок до 31.12.2026"])
    assert code == 0, err
    row = _j(out)["data"]
    assert row["case_id"] == case_id
    assert row["side"] == "plaintiff"
    assert row["name"] == "Иванов И.И."
    assert row["power_docs"] == "дов. №1-д, срок до 31.12.2026"
    assert row["authority_note"] == "по доверенности от 01.09.2026"

    code, out, err = run_cli(["--json", "case", "rep", "list", case_id], env=env)
    assert code == 0, err
    rows = _j(out)["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == row["id"]
    assert rows[0]["power_docs"] == row["power_docs"]


def test_rep_list_empty_is_explicit_empty_list(tmp_path):
    env, case_id = _mk(tmp_path, "A114-3/2026")
    code, out, err = run_cli(["--json", "case", "rep", "list", case_id], env=env)
    assert code == 0, err
    assert _j(out)["data"] == []


def test_rep_card_section(tmp_path):
    env, case_id = _mk(tmp_path, "A114-4/2026")
    _add(env, case_id, side="plaintiff", name="Иванов И.И.")
    _add(env, case_id, side="defendant", name="Петрова А.С.")
    code, out, err = run_cli(["--json", "case", "card", case_id], env=env)
    assert code == 0, err
    section = _j(out)["data"]["representatives"]
    assert section["count"] == 2
    assert section["by_side"] == {"plaintiff": 1, "defendant": 1}
    # порядок side ASC, затем id: defendant раньше plaintiff — это порядок
    # вывода, а не утверждение о приоритете стороны
    assert [i["name"] for i in section["items"]] == ["Петрова А.С.", "Иванов И.И."]
    assert section["hint"] == REPRESENTATIVE_HINT


def test_rep_manual_authority_hint_present_everywhere(tmp_path):
    """Проверка полномочий не автоматическая — hint обязан быть в выводе."""
    env, case_id = _mk(tmp_path, "A114-5/2026")
    code, out, err = _add(env, case_id)
    assert code == 0, err
    assert "вручную" in _j(out)["data"]["hint"]
    assert "дов" in _j(out)["data"]["hint"]  # про доверенность, а не про погоду

    code, out, err = run_cli(["--json", "case", "rep", "list", case_id], env=env)
    assert _j(out)["meta"]["hint"] == REPRESENTATIVE_HINT

    code, out, err = run_cli(["--json", "case", "card", case_id], env=env)
    assert _j(out)["data"]["representatives"]["hint"] == REPRESENTATIVE_HINT


def test_rep_side_validation_rejects_unknown_side(tmp_path):
    env, case_id = _mk(tmp_path, "A114-6/2026")
    # argparse не пропускает side вне {plaintiff, defendant}
    code, out, err = run_cli(["--json", "case", "rep", "add", case_id,
                              "--side", "third-party", "--name", "X"], env=env)
    assert code != 0
    code, out, err = run_cli(["--json", "case", "rep", "list", case_id], env=env)
    assert code == 0, err
    assert _j(out)["data"] == [], "отказ не должен оставить строку в таблице"


def test_rep_side_is_case_insensitive_and_normalized(tmp_path):
    """CLI choices регистрозависимы, движок — нет: 'Plaintiff' принимается и
    нормализуется, чтобы один и тот же участник не гулял по двум ключам."""
    env, case_id = _mk(tmp_path, "A114-7/2026")
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        row = add_case_representative(conn, case_id, side="Defendant", name="Сидоров")
    finally:
        conn.close()
    assert row["side"] == "defendant"
    listed = _j(run_cli(["--json", "case", "rep", "list", case_id], env=env)[1])["data"]
    assert [r["side"] for r in listed] == ["defendant"]


def test_rep_name_required(tmp_path):
    env, case_id = _mk(tmp_path, "A114-8/2026")
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        try:
            add_case_representative(conn, case_id, side="plaintiff", name="   ")
            raised = None
        except Exception as exc:
            raised = exc
    finally:
        conn.close()
    assert raised is not None, "представитель без имени — не данные, а дыра"
    assert "name" in str(raised).lower()


def test_rep_unknown_case_returns_none(tmp_path):
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        assert add_case_representative(conn, "case-absent", side="plaintiff",
                                       name="X") is None
    finally:
        conn.close()
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "rep", "add", "case-absent",
                              "--side", "plaintiff", "--name", "X"], env=env)
    assert code != 0


def test_rep_table_added_without_schema_version_bump(tmp_path):
    env, _ = _mk(tmp_path, "A114-9/2026")
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0, err
    assert _j(out)["data"]["schema_version"] == SCHEMA_VERSION == "3"
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(case_representatives)").fetchall()]
    finally:
        conn.close()
    assert "case_representatives" in tables
    assert cols == ["id", "case_id", "side", "name", "authority_note",
                   "power_docs", "added_at"]


def test_rep_migration_old_v3_db_gets_table(tmp_path):
    """Старая база v3 без case_representatives: ensure-шаг додаёт таблицу,
    SCHEMA_VERSION остаётся '3', старые строки cases не тронуты."""
    db = tmp_path / "store.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO meta VALUES ('schema_version','3');
    CREATE TABLE cases (
        case_id TEXT PRIMARY KEY, case_number TEXT NOT NULL, court TEXT NOT NULL,
        plaintiff TEXT, defendant TEXT, source_name TEXT NOT NULL,
        source_record_id TEXT, fetched_at TEXT, source_url TEXT,
        imported_at TEXT NOT NULL, status TEXT, owner TEXT, closed_at TEXT);
    INSERT INTO cases (case_id, case_number, court, source_name, imported_at)
    VALUES ('case-old','A1-1/2020','АС','t','2020-01-01T00:00:00+00:00');
    """)
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    tables_before = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "case_representatives" not in tables_before

    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        tables_after = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "case_representatives" in tables_after
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == before
        row = add_case_representative(conn, "case-old", side="defendant", name="the ownerов")
        assert row is not None and row["case_id"] == "case-old"
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(cases)").fetchall()]
    finally:
        conn.close()
    assert version == "3"
    # старая база получила и instance-колонку, и старые колонки целиком
    assert "instance" in cols
    assert all(c in cols for c in CASE_COLUMNS_V3)


def test_rep_list_order_is_side_then_id(tmp_path):
    env, case_id = _mk(tmp_path, "A114-10/2026")
    _add(env, case_id, side="defendant", name="Первый ответчик")
    _add(env, case_id, side="plaintiff", name="Первый истец")
    _add(env, case_id, side="plaintiff", name="Второй истец")
    rows = _j(run_cli(["--json", "case", "rep", "list", case_id], env=env)[1])["data"]
    assert [(r["side"], r["name"]) for r in rows] == [
        ("defendant", "Первый ответчик"),
        ("plaintiff", "Первый истец"),
        ("plaintiff", "Второй истец"),
    ]
