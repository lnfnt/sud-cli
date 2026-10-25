import json

from sud_cli import mcp_server
from sud_cli.conflict import check, normalize_name
from tests.conftest import run_cli


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _json(out):
    return json.loads(out)


def _new_case(tmp_path, number, plaintiff, defendant, owner):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "AC", "--plaintiff", plaintiff, "--defendant", defendant, "--owner", owner],
        env=env,
    )
    assert code == 0, err
    return _json(out)["data"]["case_id"], env


def test_same_party_different_owners_hit_a(tmp_path):
    cid1, env = _new_case(tmp_path, "N1", "Иванов П.П.", "D1", "O1")
    _new_case(tmp_path, "N2", "Иванов П.П.", "D2", "O2")
    # direct
    from sud_cli.db import get_connection, init_db, list_cases
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        portfolio = list_cases(conn)
    finally:
        conn.close()
    hits = check(portfolio, {"parties": ["Иванов П.П."]})
    assert any(h.get("type") == "a" or h.get("kind") == "a" or h.get("code") == "a" for h in hits)


def test_adversary_flip_hit_b(tmp_path):
    _new_case(tmp_path, "N1", "Иванов П.П.", "D1", "O1")
    _new_case(tmp_path, "N2", "D1", "Иванов П.П.", "O1")
    from sud_cli.db import get_connection, init_db, list_cases
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        portfolio = list_cases(conn)
    finally:
        conn.close()
    hits = check(portfolio, {"parties": ["Иванов П.П."]})
    assert any(h.get("type") == "b" or h.get("kind") == "b" or h.get("code") == "b" for h in hits)


def test_clean_case(tmp_path):
    _new_case(tmp_path, "N1", "A", "B", "O1")
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "conflict", "check", "--plaintiff", "Z", "--defendant", "Q"], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert data["clean"] is True
    assert data["hits"] == []
    assert data["coverage"] == "local registry names only"
    assert "disclaimer" in data


def test_normalization(tmp_path):
    _new_case(tmp_path, "N1", "Иванов П.П.", "D", "O1")
    assert normalize_name("  иванов   П.П. ") == normalize_name("Иванов П.П.")
    assert normalize_name("иванов п.п.") == normalize_name("Иванов П.П.")
    from sud_cli.db import get_connection, init_db, list_cases
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        portfolio = list_cases(conn)
    finally:
        conn.close()
    hits = check(portfolio, {"parties": ["  иванов   П.П. "]})
    assert any(h.get("type") == "c" or h.get("kind") == "c" or h.get("code") == "c" for h in hits)


def test_empty_registry_safe(tmp_path):
    from sud_cli.db import get_connection, init_db, list_cases
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        portfolio = list_cases(conn)
    finally:
        conn.close()
    hits = check(portfolio, {"parties": ["X"]})
    assert hits == []
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "conflict", "check", "--plaintiff", "X"], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert data["clean"] is True
    assert data["coverage"] == "local registry names only"


def test_mcp_schema():
    tools = mcp_server.tool_list()["tools"]
    t = next(x for x in tools if x["name"] == "sud_cli_conflict_check")
    assert t["inputSchema"].get("additionalProperties") is False


def test_case_mode(tmp_path):
    cid, env = _new_case(tmp_path, "N1", "A", "B", "O1")
    _new_case(tmp_path, "N2", "A", "C", "O2")
    code, out, err = run_cli(["--json", "conflict", "case", cid], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert data["coverage"] == "local registry names only"
    assert "disclaimer" in data
    assert any(h.get("type") == "c" or h.get("kind") == "c" or h.get("code") == "c" for h in data["hits"])


def test_candidate_hit_c(tmp_path):
    _new_case(tmp_path, "N1", "A", "B", "O1")
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "conflict", "check", "--plaintiff", "A"], env=env)
    assert code == 0
    data = _json(out)["data"]
    assert any(h.get("type") == "c" or h.get("kind") == "c" or h.get("code") == "c" for h in data["hits"])


def test_coverage_both_modes(tmp_path):
    cid, env = _new_case(tmp_path, "N1", "A", "B", "O1")
    for args in (["--json", "conflict", "check", "--plaintiff", "Z"], ["--json", "conflict", "case", cid]):
        code, out, err = run_cli(args, env=env)
        assert code == 0
        data = _json(out)["data"]
        assert data["coverage"] == "local registry names only"
        assert "disclaimer" in data


def test_mcp_call(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import os
    os.environ["SUD_CLI_STATE_DIR"] = str(tmp_path)
    try:
        run_cli(["--json", "case", "new", "--number", "N1", "--court", "AC", "--plaintiff", "A", "--defendant", "B", "--owner", "O1"], env=env)
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sud_cli_conflict_check", "arguments": {"plaintiff": "A"}}})
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["isError"] is False
        assert body["coverage"] == "local registry names only"
        assert "disclaimer" in body
        assert any(h.get("type") == "c" or h.get("kind") == "c" or h.get("code") == "c" for h in body["hits"])
    finally:
        os.environ.pop("SUD_CLI_STATE_DIR", None)
