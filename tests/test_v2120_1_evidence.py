"""the wave §1 (S-73): реестр доказательств — том/лист, sha256-целостность, verify.

Границы: таблица evidence_items появляется идемпотентным ensure-шагом init_db
(паттерн the wave case_amounts) БЕЗ bump SCHEMA_VERSION — старые пины v3 не плывут.
verify никогда не отвечает «всё хорошо» молча: per-item verdict + summary-строка.
"""
import hashlib
import json
import sqlite3

from sud_cli.db import get_connection, init_db
from tests.conftest import run_cli


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk_case(tmp_path, number="A120-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "АС г. Москвы",
         "--plaintiff", "ООО Ромашка", "--defendant", "ИП Крокодил"], env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def _add(env, case_id, extra):
    code, out, err = run_cli(
        ["--json", "case", "evidence", "add", case_id, "--title", "Договор"] + extra,
        env=env)
    return code, out, err


def test_evidence_add_with_path_hash_deterministic(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-2/2026")
    f = _write(tmp_path / "dogovor.txt", "текст договора")
    code, out, err = _add(env, case_id, ["--path", str(f)])
    assert code == 0, err
    data = _j(out)["data"]
    assert data["sha256"] == hashlib.sha256("текст договора".encode("utf-8")).hexdigest()
    assert data["title"] == "Договор"
    assert data["doc_ref"] is not None  # файл ссылается на строку case_documents


def test_evidence_verify_intact(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-3/2026")
    f = _write(tmp_path / "akt.txt", "акт сверки")
    _add(env, case_id, ["--path", str(f), "--media", "electronic"])
    code, out, err = run_cli(["--json", "case", "evidence", "verify", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert [i["verdict"] for i in data["items"]] == ["intact"]
    assert data["summary"]["intact"] == 1
    assert data["summary"]["problems"] == 0


def test_evidence_verify_changed_after_replace(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-4/2026")
    f = _write(tmp_path / "spletenie.txt", "первая редакция")
    _add(env, case_id, ["--path", str(f)])
    _write(f, "подделанная редакция")
    code, out, err = run_cli(["--json", "case", "evidence", "verify", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["items"][0]["verdict"] == "CHANGED"
    assert data["summary"]["problems"] == 1
    blob = json.dumps(data, ensure_ascii=False)
    assert "всё хорошо" not in blob.lower()  # молчаливого «ок» нет никогда


def test_evidence_verify_missing_file_after_delete(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-5/2026")
    f = _write(tmp_path / "uchastok.jpg", "данные")
    _add(env, case_id, ["--path", str(f)])
    f.unlink()
    code, out, err = run_cli(["--json", "case", "evidence", "verify", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["items"][0]["verdict"] == "MISSING_FILE"
    assert data["summary"]["problems"] == 1


def test_evidence_paper_without_path_unchecked_not_fail(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-6/2026")
    code, out, err = run_cli(
        ["--json", "case", "evidence", "add", case_id, "--title", "Распечатка",
         "--media", "paper", "--volume", "1", "--sheet", "5"], env=env)
    assert code == 0, err
    assert _j(out)["data"]["sha256"] is None
    code, out, err = run_cli(["--json", "case", "evidence", "verify", case_id], env=env)
    assert code == 0, err  # unchecked — НЕ провал
    data = _j(out)["data"]
    assert data["items"][0]["verdict"] == "unchecked"
    assert data["summary"]["problems"] == 0


def test_evidence_list_numeric_aware_sort(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-7/2026")
    for vol, sheet in [("10", "2"), ("2", "10"), ("2", "2")]:
        code, out, err = run_cli(
            ["--json", "case", "evidence", "add", case_id, "--title", f"т{vol}-{sheet}",
             "--volume", vol, "--sheet", sheet], env=env)
        assert code == 0, err
    code, out, err = run_cli(["--json", "case", "evidence", "list", case_id], env=env)
    assert code == 0, err
    rows = _j(out)["data"]
    assert [(r["volume"], r["sheet"]) for r in rows] == [("2", "2"), ("2", "10"), ("10", "2")]


def test_evidence_list_empty_explicit(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-8/2026")
    code, out, err = run_cli(["--json", "case", "evidence", "list", case_id], env=env)
    assert code == 0, err
    assert _j(out)["data"] == []


def test_evidence_card_section(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-9/2026")
    f = _write(tmp_path / "fotografiya.txt", "исходное")
    _add(env, case_id, ["--path", str(f)])
    _write(f, "подмена")
    code, out, err = run_cli(["--json", "case", "card", case_id], env=env)
    assert code == 0, err
    ev = _j(out)["data"]["evidence"]
    assert ev["count"] == 1
    assert [p["id"] for p in ev["problems"]] == [1] or ev["problems"], ev
    assert ev["problems"][0]["verdict"] == "CHANGED"


def test_evidence_add_invalid_media_rejected(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-10/2026")
    code, out, err = run_cli(
        ["--json", "case", "evidence", "add", case_id, "--title", "X",
         "--media", "floppy"], env=env)
    assert code != 0


def test_evidence_add_missing_path_errors(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-11/2026")
    code, out, err = run_cli(
        ["--json", "case", "evidence", "add", case_id, "--title", "X",
         "--path", str(tmp_path / "net-takogo.txt")], env=env)
    assert code != 0


def test_evidence_add_path_links_doc_ref_and_doc_list(tmp_path):
    env, case_id = _mk_case(tmp_path, "A120-12/2026")
    f = _write(tmp_path / "skrin.pdf", "%PDF-1.4 скриншот")
    code, out, err = _add(env, case_id, ["--path", str(f)])
    doc_ref = _j(out)["data"]["doc_ref"]
    code, out, err = run_cli(["--json", "case", "doc", "list", case_id], env=env)
    docs = _j(out)["data"]
    assert doc_ref in [d["id"] for d in docs]
    # второй add того же файла не удваивает doc_ref «ещё не добавлен» — ссылка повторима
    code, out, err = run_cli(
        ["--json", "case", "evidence", "add", case_id, "--title", "Тот же файл",
         "--path", str(f)], env=env)
    assert _j(out)["data"]["doc_ref"] == doc_ref


def test_evidence_migration_old_db_gets_table(tmp_path):
    # Старая БД, заштампованная v3: ни evidence_items, ни supersedes нет.
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
    CREATE TABLE case_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
        title TEXT, added_at TEXT NOT NULL);
    INSERT INTO cases (case_id, case_number, court, source_name, imported_at)
        VALUES ('OLD1','A40-8/2020','AC','legacy','2020-01-01T00:00:00+00:00');
    INSERT INTO case_documents (case_id, path, sha256, title, added_at)
        VALUES ('OLD1','/tmp/old.pdf','deadbeef','старый','2020-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "list"], env=env)  # прогоняет init_db
    assert code == 0, err
    conn = get_connection(tmp_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "evidence_items" in tables
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(case_documents)").fetchall()}
        assert "supersedes" in cols
        row = conn.execute("SELECT * FROM case_documents WHERE id=1").fetchone()
        assert row["supersedes"] is None
        assert row["title"] == "старый"  # старые строки не тронуты
    finally:
        conn.close()
    # и работа с новой таблицей на старой базе
    f = _write(tmp_path / "novoe.txt", "данные")
    code, out, err = _add(env, "OLD1", ["--path", str(f)])
    assert code == 0, err
