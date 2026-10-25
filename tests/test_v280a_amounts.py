"""the wave §1: реестр расходов поверх case_amounts (status/paid_at, summary, card)."""
import json
import sqlite3
from decimal import Decimal

from sud_cli.db import get_connection, init_db
from tests.conftest import run_cli


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk_case(tmp_path, number="A111-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "AC",
         "--plaintiff", "P", "--defendant", "D"], env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _add_amount(env, case_id, kind, amount):
    code, out, err = run_cli(
        ["--json", "case", "amount", "add", case_id, "--kind", kind,
         "--amount", amount], env=env)
    assert code == 0, err
    return _j(out)["data"]


def test_amount_default_status_planned_fresh_db(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-2/2026")
    added = _add_amount(env, case_id, "fee", "100")
    assert added["status"] == "planned"
    assert added["paid_at"] is None
    code, out, err = run_cli(["--json", "case", "amount", "list", case_id], env=env)
    assert code == 0, err
    assert _j(out)["data"][0]["status"] == "planned"


def test_migration_backfills_status_on_old_rows(tmp_path):
    # БД старой схемы v3: case_amounts без status/paid_at, meta='3'.
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
    CREATE TABLE case_amounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL, kind TEXT NOT NULL, amount_rub TEXT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'RUB', note TEXT, created_at TEXT NOT NULL);
    INSERT INTO cases (case_id, case_number, court, source_name, imported_at)
        VALUES ('OLD1','A40-9/2020','AC','legacy','2020-01-01T00:00:00+00:00');
    INSERT INTO case_amounts (case_id, kind, amount_rub, currency, note, created_at)
        VALUES ('OLD1','fee','100.00','RUB','legacy-note','2020-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()
    env, _ = _mk_case(tmp_path, "A111-3/2026")  # любой CLI-вызов прогоняет init_db
    code, out, err = run_cli(["--json", "case", "amount", "list", "OLD1"], env=env)
    assert code == 0, err
    rows = _j(out)["data"]
    assert len(rows) == 1
    assert rows[0]["status"] == "planned"
    assert rows[0]["paid_at"] is None
    assert rows[0]["note"] == "legacy-note"  # старые строки не тронуты


def test_alter_columns_idempotent_double_init(tmp_path):
    init_db(tmp_path)
    init_db(tmp_path)  # второй вызов не должен упасть
    conn = get_connection(tmp_path)
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(case_amounts)").fetchall()]
    finally:
        conn.close()
    assert cols.count("status") == 1
    assert cols.count("paid_at") == 1


def test_amount_set_status_paid_with_paid_at(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-4/2026")
    added = _add_amount(env, case_id, "fee", "5000")
    code, out, err = run_cli(
        ["--json", "case", "amount", "set", case_id, str(added["id"]),
         "--status", "paid", "--paid-at", "2026-10-20"], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["status"] == "paid"
    assert data["paid_at"] == "2026-10-20"


def test_amount_set_invalid_paid_at_rejected(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-5/2026")
    added = _add_amount(env, case_id, "fee", "1")
    code, out, err = run_cli(
        ["--json", "case", "amount", "set", case_id, str(added["id"]),
         "--status", "paid", "--paid-at", "20.10.2026"], env=env)
    assert code != 0
    data = _j(out if out.strip() else err)
    assert "INVALID_ARGUMENTS" in json.dumps(data)


def test_amount_set_refunded_prints_manual_review(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-6/2026")
    added = _add_amount(env, case_id, "fee", "35000")
    code, out, err = run_cli(
        ["--json", "case", "amount", "set", case_id, str(added["id"]),
         "--status", "refunded"], env=env)
    assert code == 0, err  # refunded принимается, но маркируется
    blob = json.dumps(_j(out), ensure_ascii=False)
    assert "manual_review" in blob
    assert "333.40" in blob
    assert _j(out)["data"]["status"] == "refunded"


def test_amount_set_unknown_id_fails(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-7/2026")
    code, out, err = run_cli(
        ["--json", "case", "amount", "set", case_id, "9999", "--status", "paid"], env=env)
    assert code != 0


def test_summary_decimal_exact(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-8/2026")
    a = _add_amount(env, case_id, "x", "0.1")
    b = _add_amount(env, case_id, "x", "0.2")
    for row in (a, b):
        code, out, err = run_cli(
            ["--json", "case", "amount", "set", case_id, str(row["id"]),
             "--status", "paid"], env=env)
        assert code == 0, err
    code, out, err = run_cli(["--json", "case", "amount", "summary", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    row = [r for r in data["rows"] if r["kind"] == "x" and r["status"] == "paid"]
    assert len(row) == 1
    assert Decimal(row[0]["total"]) == Decimal("0.3")
    assert str(row[0]["total"]) == "0.3"  # без float-артефактов 0.30000000000000004
    assert Decimal(data["totals_by_status"]["paid"]) == Decimal("0.3")


def test_summary_grouping_kind_x_status(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-9/2026")
    fee_planned = _add_amount(env, case_id, "fee", "100.00")
    fee_paid = _add_amount(env, case_id, "fee", "50")
    run_cli(["--json", "case", "amount", "set", case_id, str(fee_paid["id"]),
             "--status", "paid", "--paid-at", "2026-10-01"], env=env)
    travel_paid = _add_amount(env, case_id, "travel", "20")
    code, out, err = run_cli(
        ["--json", "case", "amount", "set", case_id, str(travel_paid["id"]),
         "--status", "paid"], env=env)
    assert code == 0, err
    code, out, err = run_cli(["--json", "case", "amount", "summary", case_id], env=env)
    data = _j(out)["data"]
    got = {(r["kind"], r["status"]): Decimal(r["total"]) for r in data["rows"]}
    assert got == {("fee", "planned"): Decimal("100.00"),
                   ("fee", "paid"): Decimal("50"),
                   ("travel", "paid"): Decimal("20")}
    assert Decimal(data["totals_by_status"]["planned"]) == Decimal("100.00")
    assert Decimal(data["totals_by_status"]["paid"]) == Decimal("70")
    assert code == 0


def test_summary_empty_explicit_list(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-10/2026")
    code, out, err = run_cli(["--json", "case", "amount", "summary", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["rows"] == []
    assert data["disclaimer"].startswith("Расчёт справочный")


def test_card_shows_status_and_paid_at(tmp_path):
    env, case_id = _mk_case(tmp_path, "A111-11/2026")
    added = _add_amount(env, case_id, "fee", "77")
    run_cli(["--json", "case", "amount", "set", case_id, str(added["id"]),
             "--status", "paid", "--paid-at", "2026-10-05"], env=env)
    code, out, err = run_cli(["--json", "case", "card", case_id], env=env)
    assert code == 0, err
    amounts = _j(out)["data"]["amounts"]
    assert amounts[0]["status"] == "paid"
    assert amounts[0]["paid_at"] == "2026-10-05"
