"""the wave §2 (S-72): ревизии документов дела — supersedes-колонка, история, текущий.

Повторный add того же path тому же делу = новая строка-ревизия с supersedes=id
предыдущей head-записи; «текущий» = max(added_at,id) среди не-superseded.
Существующие строки не мутируются никогда; разные файлы не связываются.
"""
import json

from sud_cli.db import get_connection, init_db
from tests.conftest import run_cli


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk_case(tmp_path, number="A130-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "АС", "--plaintiff", "П",
         "--defendant", "Д"], env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _doc_add(env, case_id, path, extra=()):
    code, out, err = run_cli(
        ["--json", "case", "doc", "add", case_id, "--path", str(path)] + list(extra),
        env=env)
    return code, out, err


def _files(tmp_path):
    a = tmp_path / "isk.txt"
    a.write_text("исковая редакция 1", encoding="utf-8")
    b = tmp_path / "otzyv.txt"
    b.write_text("отзыв", encoding="utf-8")
    return a, b


def test_doc_double_add_creates_linked_revisions(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-2/2026")
    a, _ = _files(tmp_path)
    code, out, err = _doc_add(env, case_id, a)
    first = _j(out)["data"]
    assert first["supersedes"] is None
    a.write_text("исковая редакция 2", encoding="utf-8")
    code, out, err = _doc_add(env, case_id, a)
    second = _j(out)["data"]
    assert second["id"] != first["id"]
    assert second["supersedes"] == first["id"]
    code, out, err = run_cli(["--json", "case", "doc", "list", case_id], env=env)
    assert len(_j(out)["data"]) == 2


def test_doc_history_chain_newest_first_with_hashes(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-3/2026")
    a, _ = _files(tmp_path)
    _, out1, _ = _doc_add(env, case_id, a)
    h1 = _j(out1)["data"]["sha256"]
    a.write_text("исковая редакция 2", encoding="utf-8")
    _, out2, _ = _doc_add(env, case_id, a)
    h2 = _j(out2)["data"]["sha256"]
    code, out, err = run_cli(["--json", "case", "doc", "history", case_id], env=env)
    assert code == 0, err
    rows = _j(out)["data"]
    assert [r["id"] for r in rows] == sorted([r["id"] for r in rows], reverse=True)
    assert {r["sha256"] for r in rows} == {h1, h2}
    assert rows[0]["current"] is True
    assert rows[1]["current"] is False
    assert rows[0]["supersedes"] == rows[1]["id"]


def test_doc_current_is_latest_non_superseded(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-4/2026")
    a, _ = _files(tmp_path)
    _, out1, _ = _doc_add(env, case_id, a)
    first = _j(out1)["data"]
    a.write_text("v2", encoding="utf-8")
    _, out2, _ = _doc_add(env, case_id, a)
    second = _j(out2)["data"]
    a.write_text("v3", encoding="utf-8")
    _, out3, _ = _doc_add(env, case_id, a)
    third = _j(out3)["data"]
    code, out, err = run_cli(["--json", "case", "card", case_id], env=env)
    card = _j(out)["data"]
    assert [d["id"] for d in card["documents"]] == [first["id"], second["id"], third["id"]]
    assert [d["id"] for d in card["documents_current"]] == [third["id"]]


def test_doc_different_files_not_linked(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-5/2026")
    a, b = _files(tmp_path)
    _, out_a, _ = _doc_add(env, case_id, a)
    _, out_b, _ = _doc_add(env, case_id, b)
    assert _j(out_a)["data"]["supersedes"] is None
    assert _j(out_b)["data"]["supersedes"] is None


def test_doc_chain_linear_no_branching(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-6/2026")
    a, _ = _files(tmp_path)
    _, out1, _ = _doc_add(env, case_id, a)
    r1 = _j(out1)["data"]
    for text in ("v2", "v3", "v4"):
        a.write_text(text, encoding="utf-8")
        _, outn, _ = _doc_add(env, case_id, a)
    code, out, err = run_cli(["--json", "case", "doc", "history", case_id], env=env)
    rows = _j(out)["data"]
    links = {r["id"]: r["supersedes"] for r in rows}
    assert links[rows[0]["id"]] == rows[1]["id"]
    assert links[rows[1]["id"]] == rows[2]["id"]
    assert links[rows[2]["id"]] == rows[3]["id"]
    assert links[r1["id"]] is None
    current = [r for r in rows if r["current"]]
    assert len(current) == 1


def test_doc_explicit_doc_id_supersedes(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-7/2026")
    a, b = _files(tmp_path)
    _, out_a, _ = _doc_add(env, case_id, a)
    a_id = _j(out_a)["data"]["id"]
    code, out, err = _doc_add(env, case_id, b, extra=["--doc-id", str(a_id)])
    assert code == 0, err
    rev = _j(out)["data"]
    assert rev["supersedes"] == a_id
    code, out, err = _doc_add(env, case_id, b, extra=["--doc-id", "99999"])
    assert code != 0  # несуществующий id — явная ошибка, не магия


def test_doc_history_empty_returns_list(tmp_path):
    env, case_id = _mk_case(tmp_path, "A130-8/2026")
    code, out, err = run_cli(["--json", "case", "doc", "history", case_id], env=env)
    assert code == 0, err
    assert _j(out)["data"] == []


def test_doc_supersedes_migration_old_rows_untouched(tmp_path):
    import sqlite3
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
        VALUES ('OLD1','A40-7/2021','AC','legacy','2021-01-01T00:00:00+00:00');
    INSERT INTO case_documents (case_id, path, sha256, title, added_at)
        VALUES ('OLD1','/tmp/a.pdf','aaa111','документ-старый','2021-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        row = conn.execute("SELECT * FROM case_documents WHERE id=1").fetchone()
        assert row["supersedes"] is None
        assert row["title"] == "документ-старый"
        assert row["sha256"] == "aaa111"
    finally:
        conn.close()
