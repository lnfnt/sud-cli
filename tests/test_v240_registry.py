import hashlib
import json
from pathlib import Path

from sud_cli import mcp_server
from sud_cli.db import SCHEMA_VERSION, get_connection, init_db
from tests.conftest import run_cli


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _json(out):
    return json.loads(out)


def test_doctor_schema_v3(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0
    data = _json(out)
    assert data["data"]["schema_version"] == "3"


def test_case_new_and_duplicate(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-1/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D", "--owner", "I", "--status", "new"], env=env)
    assert code == 0
    first = _json(out)
    case_id = first["data"]["case_id"]
    # duplicate
    code2, out2, err2 = run_cli(["--json", "case", "new", "--number", "A40-1/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=env)
    data2 = _json(out2)
    assert data2["data"]["status"] == "duplicate_window" or data2["status"] == "error"
    assert data2.get("data", {}).get("case_id") == case_id or data2.get("case_id") == case_id


def test_invalid_status(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-2/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D", "--status", "bogus"], env=env)
    # could be exit 1
    data = _json(out if out.strip() else err)
    assert "invalid_status" in json.dumps(data)


def test_set_partial_and_unknown(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-3/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=env)
    case_id = _json(out)["data"]["case_id"]
    code, out, err = run_cli(["--json", "case", "set", case_id, "--status", "filed"], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert data["status"] == "filed"
    # unknown field via CLI not possible; check MCP set unknown reject
    resp = mcp_server.handle_request({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"sud_cli_case_set","arguments":{"case_id":case_id,"foo":1}}})
    assert resp["result"]["isError"] is True


def test_amounts_decimal(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-4/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=env)
    case_id = _json(out)["data"]["case_id"]
    code, out, err = run_cli(["--json", "case", "amount", "add", case_id, "--kind", "иск", "--amount", "1234,56"], env=env)
    assert code == 0
    assert _json(out)["data"]["amount_rub"] == "1234.56"
    code, out, err = run_cli(["--json", "case", "amount", "add", case_id, "--kind", "x", "--amount", "abc"], env=env)
    data = _json(out if out.strip() else err)
    assert "invalid_amount" in json.dumps(data)


def test_doc_add(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-5/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=env)
    case_id = _json(out)["data"]["case_id"]
    f = tmp_path / "doc.txt"
    f.write_text("hello")
    code, out, err = run_cli(["--json", "case", "doc", "add", case_id, "--path", str(f), "--title", "T"], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert data["sha256"] == hashlib.sha256(b"hello").hexdigest()
    # not found
    code, out, err = run_cli(["--json", "case", "doc", "add", case_id, "--path", str(tmp_path/"nope.txt")], env=env)
    data = _json(out if out.strip() else err)
    assert "doc_not_found" in json.dumps(data)


def test_note_supersede_chain(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-6/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=env)
    case_id = _json(out)["data"]["case_id"]
    code, out, err = run_cli(["--json", "case", "note", "add", case_id, "--text", "first", "--author", "A"], env=env)
    note_id = _json(out)["data"]["id"]
    code, out, err = run_cli(["--json", "case", "note", "supersede", str(note_id), "--text", "second"], env=env)
    assert code == 0
    new_note = _json(out)["data"]
    assert new_note["supersedes"] == note_id
    # list
    code, out, err = run_cli(["--json", "case", "note", "list", case_id], env=env)
    notes = _json(out)["data"]
    assert len(notes) == 2


def test_list_filters(tmp_path):
    env = _env(tmp_path)
    for st in ["new", "filed"]:
        run_cli(["--json", "case", "new", "--number", f"A40-{st}/2026", "--court", "AC Moscow", "--plaintiff", "Истец", "--defendant", "Ответчик", "--status", st, "--owner", "O"], env=env)
    code, out, err = run_cli(["--json", "case", "list", "--status", "filed", "--owner", "O", "--court", "Moscow", "--party", "Истец"], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert len(data) == 1
    assert data[0]["status"] == "filed"


def test_card_empty_sections(tmp_path):
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-7/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=env)
    case_id = _json(out)["data"]["case_id"]
    code, out, err = run_cli(["--json", "case", "card", case_id], env=env)
    assert code == 0
    card = _json(out)["data"]
    for key in ("amounts", "documents", "notes", "events"):
        assert card.get(key) == [], key
    # empty lists not None
    assert card.get("next_deadline") is None


def test_migration_v2(tmp_path):
    import sqlite3
    state = tmp_path
    db = state / "store.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO meta VALUES ('schema_version','2');
    CREATE TABLE cases (
        case_id TEXT PRIMARY KEY,
        case_number TEXT NOT NULL,
        court TEXT NOT NULL,
        plaintiff TEXT,
        defendant TEXT,
        source_name TEXT NOT NULL,
        source_record_id TEXT,
        fetched_at TEXT,
        source_url TEXT,
        imported_at TEXT NOT NULL
    );
    INSERT INTO cases (case_id, case_number, court, plaintiff, defendant, source_name, imported_at)
    VALUES ('c1','N','C','P','D','src','2026-01-01');
    """)
    conn.commit()
    conn.close()
    init_db(state)
    conn = get_connection(state)
    row = conn.execute("SELECT * FROM cases WHERE case_id='c1'").fetchone()
    assert row is not None
    assert dict(row)["court"] == "C"
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    # new tables exist
    for t in ("case_amounts", "case_documents", "case_notes"):
        assert conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone() is not None
    conn.close()


def test_mcp_schemas():
    tools = mcp_server.tool_list()["tools"]
    names = {t["name"] for t in tools}
    for want in {"sud_cli_case_new", "sud_cli_case_set", "sud_cli_case_list", "sud_cli_case_card",
                 "sud_cli_case_amount_add", "sud_cli_case_doc_add", "sud_cli_case_note_add"}:
        assert want in names
    # no duplicates
    all_names = [t["name"] for t in tools]
    assert len(all_names) == len(set(all_names))
    for t in tools:
        if t["name"].startswith("sud_cli_case_") and t["name"] in {"sud_cli_case_new","sud_cli_case_set","sud_cli_case_list","sud_cli_case_card","sud_cli_case_amount_add","sud_cli_case_doc_add","sud_cli_case_note_add"}:
            assert t["inputSchema"].get("additionalProperties") is False
