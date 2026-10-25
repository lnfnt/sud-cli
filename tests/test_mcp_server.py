"""v2.2.0 MCP-контракт: типизированные схемы + новые тулзы (рецензия colleague-legal 21.10).

v2.3.0 (письмо A2A colleague-legal 001a1608): sud_cli_case_diff + sud_cli_npa_compare,
19 инструментов; доменные ошибки (DiffError/SearchError) не должны ронять
serve_stdio — они конвертируются в isError=True.
"""
import json
from sud_cli import mcp_server


def _call(name, arguments, request_id=7):
    return mcp_server.handle_request({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def test_every_tool_has_declared_schema():
    tools = mcp_server.tool_list()["tools"]
    assert tools, "пустой список"
    # v2.11.0 W6a §1: 35 -> 45 — плановая волна паритета добавила ровно 10
    # офлайн-утилитных тулов (anchor/confirm/waive/list/audit/autofill, agenda,
    # ics, reminders due, case-search alerts); live-тулы не тронуты.
    # v2.12.0 the wave §4: 45 -> 46 — ровно один новый read-тул sud_cli_form_new
    # (скелеты форм); счётчик двигается планово, обоснование в том же духе.
    # v2.13.0 история §5: 46 -> 58 — волна «портфель и данные» добавила ровно 12
    # тулов по списку TOOL_CLI_PATH (chain, rep add/list, conflicts, report
    # portfolio/case, metrics, backup create/verify/restore, case export/purge).
    # live-тулы не тронуты, новых сетевых команд нет.
    assert len(tools) == 65, (
        f"ожидалось 63 инструмента (W6a: 45 + the wave: 1 + the wave: 12 + egrul 1 + fssp 3 + license 1), получено {len(tools)}"
    )
    for t in tools:
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        props = schema.get("properties") or {}
        assert props, f"{t['name']}: пустой inputSchema (регресс v2.1.0)"
        assert schema.get("additionalProperties") is False, t["name"]


def test_new_tools_present():
    names = {t["name"] for t in mcp_server.tool_list()["tools"]}
    for want in {"sud_cli_case_history", "sud_cli_payment", "sud_cli_deferral",
                 "sud_cli_efrsb_companies", "sud_cli_efrsb_persons",
                 "sud_cli_efrsb_publications", "sud_cli_efrsb_managers",
                 "sud_cli_sudrf_live", "sud_cli_mos_live",
                 "sud_cli_case_diff", "sud_cli_npa_compare"}:
        assert want in names, want


def test_initialize_reports_v23():
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["serverInfo"]["version"] == __import__("sud_cli").__version__  # v2.3.2: патч-версия не цементируется


def test_payment_tool_offline_call():
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "sud_cli_payment", "arguments": {
            "jurisdiction": "arbitration", "amount": 6000, "court": "АС г. Москвы",
            "defendant": "ООО «Ромашка»"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["status"] == "ok" and body["amount_rub"] == 6000


def test_unknown_property_fails_closed():
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "sud_cli_deferral", "arguments": {
            "applicant": "Иванов", "court": "АС", "amount": 1, "reason": "нет денег",
            "typo_param": 1}}})
    # TypeError от функции: неизвестный kwarg не должен молча съедаться
    assert resp["result"]["isError"] is True


def test_live_tool_blocked_without_env(monkeypatch):
    monkeypatch.delenv("SUD_CLI_BRIDGE_LIVE", raising=False)
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "sud_cli_sudrf_live", "arguments": {"case_id": "А40-12345-2026"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["status"] == "blocked", "live без гейта обязан блокироваться, не спавнить subprocess"


# ---------------------------------------------------------------------------
# v2.3.0: sud_cli_case_diff (письмо colleague-legal 001a1608, вариант «в»)
# ---------------------------------------------------------------------------

def _write_snapshot(path, case_number, status, hearing_date):
    path.write_text(json.dumps({
        "case_number": case_number,
        "status": status,
        "court": "АС г. Москвы",
        "judge": "Судья Иванов И.И.",
        "next_hearing": {"date": hearing_date, "time": "10:00", "hall": "405", "type": "объявление решения"},
        "last_event": {"date": "2026-10-01", "event_type": "принятие", "document_url": None},
        "events": [],
    }, ensure_ascii=False), encoding="utf-8")


def test_case_diff_tool_happy_path(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_snapshot(old, "А12-345/2024", "Принято", "2026-11-01")
    _write_snapshot(new, "А12-345/2024", "Назначено", "2026-11-15")
    resp = _call("sud_cli_case_diff", {"old": str(old), "new": str(new)})
    assert resp["result"]["isError"] is False
    body = _body(resp)
    assert body["changed"] is True
    paths = {c["path"] for c in body["changes"]}
    assert "status" in paths, body["changes"]
    assert "next_hearing.date" in paths, body["changes"]
    status_change = next(c for c in body["changes"] if c["path"] == "status")
    assert status_change["before"] == "Принято" and status_change["after"] == "Назначено"


def test_case_diff_tool_accepts_snapshot_aliases(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_snapshot(old, "А12-345/2024", "Принято", "2026-11-01")
    _write_snapshot(new, "А12-345/2024", "Принято", "2026-11-01")
    resp = _call("sud_cli_case_diff",
                 {"old_snapshot": str(old), "new_snapshot": str(new)})
    assert resp["result"]["isError"] is False
    assert _body(resp)["changed"] is False


def test_case_diff_tool_requires_paths():
    resp = _call("sud_cli_case_diff", {"new": "/nonexistent/new.json"})
    assert resp["result"]["isError"] is True
    assert "old snapshot path required" in resp["result"]["content"][0]["text"]


def test_case_diff_different_cases_is_error_and_server_survives(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_snapshot(old, "А12-345/2024", "Принято", "2026-11-01")
    _write_snapshot(new, "А99-999/2025", "Принято", "2026-11-01")
    resp = _call("sud_cli_case_diff", {"old": str(old), "new": str(new)})
    text = resp["result"]["content"][0]["text"]
    # DiffError сконвертирован в ValueError: isError, а не крах serve_stdio
    assert resp["result"]["isError"] is True
    assert "different cases" in text, text
    # сервер продолжает обслуживать: валидный запрос после ошибки проходит
    after = _call("sud_cli_case_diff", {"old": str(old), "new": str(old)}, request_id=8)
    assert after["result"]["isError"] is False
    assert _body(after)["changed"] is False


# ---------------------------------------------------------------------------
# v2.3.0: sud_cli_npa_compare (аддитивно, вариант «а»)
# ---------------------------------------------------------------------------

def _seed_npa(tmp_path, doc_id):
    """Тот же путь сидинга, что в tests/test_npa.py: CLI `import npa`."""
    from tests.conftest import run_cli
    fixture = tmp_path / "npa.json"
    fixture.write_text(json.dumps({"documents": [
        {"doc_id": doc_id, "edition": "v1", "title": "Синтетическое правило",
         "doc_date": "2026-01-01", "body": "Исходная редакция."},
        {"doc_id": doc_id, "edition": "v2", "title": "Синтетическое правило (изм.)",
         "doc_date": "2026-06-01", "body": "Изменённая редакция с дополнениями."},
    ]}, ensure_ascii=False), encoding="utf-8")
    code, out, err = run_cli(["--json", "import", "npa", str(fixture)],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code == 0, err


def test_npa_compare_tool_happy_path(tmp_path, monkeypatch):
    _seed_npa(tmp_path, "MCP-SYNTH-001")
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    resp = _call("sud_cli_npa_compare",
                 {"doc_id": "MCP-SYNTH-001", "edition1": "v1", "edition2": "v2"})
    assert resp["result"]["isError"] is False
    body = _body(resp)
    assert body["edition1"] == "v1" and body["edition2"] == "v2"
    assert body["doc_id"] == "MCP-SYNTH-001"
    assert body["same_body"] is False
    assert body["body2_length"] > body["body1_length"]


def test_npa_compare_tool_missing_edition_is_error(tmp_path, monkeypatch):
    _seed_npa(tmp_path, "MCP-SYNTH-002")
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    resp = _call("sud_cli_npa_compare",
                 {"doc_id": "MCP-SYNTH-002", "edition1": "v1", "edition2": "v9"})
    # SearchError -> ValueError: клиент получает isError, цикл сервера жив
    assert resp["result"]["isError"] is True
    assert "not found" in resp["result"]["content"][0]["text"].lower()
