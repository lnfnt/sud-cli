import json
from sud_cli import mcp_server
from tests.conftest import run_cli


def E(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def J(out):
    return json.loads(out)


def make_case(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-1/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=e)
    assert code == 0, err
    return e, J(out)["data"]["case_id"]


def test_no_anchors_all_needs(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    assert code == 0, err
    pts = J(out)["data"]["points"]
    assert pts and all(p["due_at"] is None for p in pts)
    assert all(p["needs_anchor"] is True for p in pts)
    assert all(p["base_event"] == "MISSING" for p in pts)


def test_anchor_one_due(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "e", "--by", "lex"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    assert code == 0, err
    pts = J(out)["data"]["points"]
    due = [p for p in pts if p["due_at"] is not None]
    assert len(due) == 1
    assert due[0]["key"] == "as_appeal_decision"
    assert all(p["due_at"] is None for p in pts if p["key"] != "as_appeal_decision")


def test_as_of_past_no_effect(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "e", "--by", "lex"], env=e)
    code1, out1, _ = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    code2, out2, _ = run_cli(["--json", "deadlines", "matrix", cid, "--as-of", "2020-01-01"], env=e)
    assert code1 == 0 and code2 == 0
    assert J(out1)["data"]["points"] == J(out2)["data"]["points"]


def test_instance_override_apk(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "e", "--by", "lex"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid, "--instance", "apk"], env=e)
    assert code == 0, err
    pts = J(out)["data"]["points"]
    assert len(pts) == 8  # +cure row (юридическая проверка 24.10)
    assert J(out)["data"]["instance"] == "apk"


def test_instance_override_invalid(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid, "--instance", "foo"], env=e)
    assert code != 0


def test_missing_filter(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "anchor", cid, "--key", "as_appeal_decision", "--event-date", "2026-10-09", "--method", "вручение", "--evidence", "e", "--by", "lex"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid, "--missing"], env=e)
    assert code == 0, err
    pts = J(out)["data"]["points"]
    assert pts and all(p["needs_anchor"] for p in pts)


def test_state_visible(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    assert code == 0, err
    pt = next(p for p in J(out)["data"]["points"] if p["key"] == "as_appeal_decision")
    assert pt["state"] == "computed"


def test_disclaimer_present(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    assert code == 0, err
    assert "disclaimer" in J(out)["data"]


def test_norm_from_rules(tmp_path):
    e, cid = make_case(tmp_path)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    assert code == 0, err
    pt = J(out)["data"]["points"][0]
    assert pt["norm"]


def test_base_event_assumed(tmp_path):
    e, cid = make_case(tmp_path)
    run_cli(["--json", "deadlines", "calculate", cid, "as_appeal_decision", "--event-date", "2026-10-09"], env=e)
    code, out, err = run_cli(["--json", "deadlines", "matrix", cid], env=e)
    assert code == 0, err
    pt = next(p for p in J(out)["data"]["points"] if p["key"] == "as_appeal_decision")
    assert pt["base_event"] == "assumed"


def test_mcp_schema():
    tools = mcp_server.tool_list()["tools"]
    t = next(x for x in tools if x["name"] == "sud_cli_deadlines_matrix")
    schema = t["inputSchema"]
    assert schema.get("additionalProperties") is False


def test_mcp_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "case", "new", "--number", "A40-1/2026", "--court", "AC", "--plaintiff", "P", "--defendant", "D"], env=e)
    cid = J(out)["data"]["case_id"]
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sud_cli_deadlines_matrix", "arguments": {"case_id": cid}}})
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["case_id"] == cid
    assert "disclaimer" in body
