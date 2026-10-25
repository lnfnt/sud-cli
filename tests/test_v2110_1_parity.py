"""W6a §1 (S-80): MCP/CLI паритет + permission + needs_confirmation + идемпотентность.

Пинов 10+ требует ТЗ; каждый тест — контракт волны, а не воспроизводство
реализации:
- permission обязателен во ВСЕХ 45 схемах (read|write);
- write-тул без confirm: true = needs_confirmation БЕЗ записи (проверяется
  чтением БД, а не только телом ответа);
- идемпотентный повтор = ПЕРВЫЙ результат + idempotent_replay=true в теле,
  эффект ровно один, реестр 0600, окно 10 минут;
- паритет: белый список офлайн-утилит закрыт инструментами, карта
  TOOL_CLI_PATH соответствует дереву argparse; live-сетевые команды новым
  волновым тулом не экспонированы.
"""
import json
import os
import stat

import pytest

from sud_cli import mcp_server
from tests.conftest import run_cli

W6A_NEW_TOOLS = {
    "sud_cli_deadlines_anchor", "sud_cli_deadlines_confirm",
    "sud_cli_deadlines_waive", "sud_cli_deadlines_list",
    "sud_cli_deadlines_audit", "sud_cli_deadlines_autofill",
    "sud_cli_calendar_agenda", "sud_cli_calendar_ics",
    "sud_cli_reminders_due", "sud_cli_case_search_alerts",
}

#: история §5 (v2.13.0): волна «портфель и данные» — 12 тулов, каждый с живой
#: CLI-командой в TOOL_CLI_PATH. Список держится здесь, чтобы пин количества
#: тулов считался по волнам, а не «по факту».
W6B2_NEW_TOOLS = {
    "sud_cli_deadlines_chain", "sud_cli_case_rep_add", "sud_cli_case_rep_list",
    "sud_cli_calendar_conflicts", "sud_cli_report_portfolio", "sud_cli_report_case",
    "sud_cli_metrics", "sud_cli_backup_create", "sud_cli_backup_verify",
    "sud_cli_backup_restore", "sud_cli_case_export", "sud_cli_case_purge",
}


def _call(name, arguments, request_id=7):
    return mcp_server.handle_request({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def _case_args(number="A110-1/2026"):
    return {"number": number, "court": "АС г. Москвы",
            "plaintiff": "ООО Альфа", "defendant": "ИП Бета"}


@pytest.fixture()
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- permission --

def test_permission_field_on_every_tool():
    for t in mcp_server.TOOLS:
        assert t.get("permission") in ("read", "write"), t["name"]
    listed = mcp_server.tool_list()["tools"]
    for t in listed:
        assert t.get("permission") in ("read", "write"), t["name"]


def test_write_permission_matches_state_changing_tools():
    writes = {t["name"] for t in mcp_server.TOOLS if t["permission"] == "write"}
    assert writes == mcp_server.WRITE_TOOLS
    # read-тулы: вычисление ничего не меняет — в частности старый
    # sud_cli_deadlines (чистый расчёт в MCP-диспатче) остаётся read.
    assert mcp_server.tool_permission("sud_cli_deadlines") == "read"
    assert mcp_server.tool_permission("sud_cli_deadlines_anchor") == "write"


def test_write_tool_schemas_declare_confirm_and_idempotency_key():
    for t in mcp_server.TOOLS:
        if t["permission"] != "write":
            continue
        props = t["schema"]["properties"]
        assert "confirm" in props, t["name"]
        assert "idempotency_key" in props, t["name"]
        assert t["schema"]["additionalProperties"] is False, t["name"]


# ------------------------------------------------------- needs_confirmation --

def test_write_tool_without_confirm_returns_needs_confirmation(state_env):
    resp = _call("sud_cli_case_new", _case_args())
    body = _body(resp)
    assert resp["result"]["isError"] is False, body
    assert body["status"] == "needs_confirmation"
    assert body["permission"] == "write"
    # запись не произведена: проверяем чтением БД, а не только телом ответа
    resp2 = _call("sud_cli_case_list", {})
    assert _body(resp2) == []


def test_write_tool_rejects_unknown_field_even_without_confirm(state_env):
    """Fail-closed v2.4.0 не смещён гейтом: неизвестное поле — ошибка и без confirm."""
    resp = _call("sud_cli_case_set", {"case_id": "does-not-matter", "foo": 1})
    assert resp["result"]["isError"] is True
    assert "unknown fields" in resp["result"]["content"][0]["text"]


def test_write_tool_executes_with_confirm_true(state_env):
    resp = _call("sud_cli_case_new", {**_case_args(), "confirm": True})
    body = _body(resp)
    assert body.get("case_id"), body
    resp2 = _call("sud_cli_case_list", {})
    assert len(_body(resp2)) == 1


def test_anchor_needs_confirmation_then_writes(state_env):
    resp = _call("sud_cli_case_new", {**_case_args("A110-2/2026"), "confirm": True})
    case_id = _body(resp)["case_id"]
    anchor_args = {"case_id": case_id, "key": "as_appeal_decision",
                   "event_date": "2026-03-02", "method": "вручение",
                   "evidence": "уведомление №1"}
    body = _body(_call("sud_cli_deadlines_anchor", anchor_args))
    assert body["status"] == "needs_confirmation"
    # без confirm запись state не создана
    body = _body(_call("sud_cli_deadlines_list", {"case_id": case_id}))
    assert body == []
    body = _body(_call("sud_cli_deadlines_anchor", {**anchor_args, "confirm": True}))
    assert body["state"] == "computed"
    assert body["basis"]["method"] == "вручение"
    assert body["due_at"], body


# ------------------------------------------------------------ идемпотентность --

def test_idempotent_replay_returns_first_result_one_effect(state_env):
    args = {**_case_args("A110-3/2026"), "confirm": True,
            "idempotency_key": "wave-6a-3"}
    first = _body(_call("sud_cli_case_new", args))
    assert first.get("case_id")
    assert "idempotent_replay" not in first
    second = _body(_call("sud_cli_case_new", args))
    assert second["idempotent_replay"] is True
    assert second["case_id"] == first["case_id"]
    cases = _body(_call("sud_cli_case_list", {}))
    assert len(cases) == 1, "повтор не должен удваивать запись"


def test_idempotency_window_expiry_stops_replay(state_env):
    from sud_cli import idempotency
    import time as _t
    key = "w6-window"
    idempotency.record(state_env, "sud_cli_case_new", key, {"case_id": "c-1"},
                       now=_t.time() - (idempotency.WINDOW_SECONDS + 5))
    assert idempotency.lookup(state_env, "sud_cli_case_new", key) is None
    idempotency.record(state_env, "sud_cli_case_new", key, {"case_id": "c-2"},
                       now=_t.time())
    hit = idempotency.lookup(state_env, "sud_cli_case_new", key)
    assert hit == {"case_id": "c-2"}


def test_idempotency_registry_file_is_0600(state_env):
    from sud_cli import idempotency
    _call("sud_cli_case_new", {**_case_args("A110-4/2026"), "confirm": True,
                               "idempotency_key": "mode-check"})
    path = idempotency.registry_path(state_env)
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)


def test_idempotency_distinct_keys_are_distinct_records(state_env):
    a = _body(_call("sud_cli_case_new", {**_case_args("A110-5/2026"),
                                         "confirm": True, "idempotency_key": "k-5a"}))
    b = _body(_call("sud_cli_case_new", {**_case_args("A110-6/2026"),
                                         "confirm": True, "idempotency_key": "k-5b"}))
    assert a["case_id"] != b["case_id"]
    assert "idempotent_replay" not in b


# ------------------------------------------------------------------- паритет --

def _parser_tree(parser):
    """(путь команды tuple -> argparse-парсер) всего дерева subparsers."""
    out = {}

    def walk(p, prefix):
        for action in p._actions:
            if hasattr(action, "choices") and isinstance(action.choices, dict) \
                    and getattr(action, "dest", None) not in (None, "func"):
                for name, sub in action.choices.items():
                    if isinstance(sub, __import__("argparse").ArgumentParser):
                        path = prefix + (name,)
                        out[path] = sub
                        walk(sub, path)
    walk(parser, ())
    return out


def test_tool_count_pin_v2110():
    # W6a §1: было 35 (+verify_quote в v2.9.0) -> стало 45 (+10 офлайн-утилит
    # паритета). Счётчик растёт предсказуемо ровно на этот список.
    # the wave §4: +1 ровно (read-тул sud_cli_form_new) — пин двигается явно,
    # а не «по факту списка волны», иначе формула перестаёт считать волну.
    # история §5: +12 (портфель/данные/ПДн), список волны — W6B2_NEW_TOOLS ниже.
    # v2.14.0 S-03: +1 egrul; v2.15.0 S-61: +3 fssp; v0.1.1 §13-интерфейс: +1 license; v0.1.1 W7: +2
    assert len(mcp_server.TOOLS) == 35 + len(W6A_NEW_TOOLS) + 1 + len(W6B2_NEW_TOOLS) + 7


def test_new_tools_present_in_tools_list():
    names = {t["name"] for t in mcp_server.tool_list()["tools"]}
    assert W6A_NEW_TOOLS <= names


def test_parity_whitelist_exposed_and_commands_real():
    """Белый список офлайн-утилит закрыт MCP; каждый путь — реальная команда CLI."""
    REQUIRED_OFFLINE = {
        ("fees",), ("limitation",), ("interest",), ("payment",), ("deferral",),
        ("timeline-audit",), ("npa-as-of",), ("fee-exemption",), ("fee-refund",),
        ("fee-indexation",), ("verify", "quote"), ("npa", "compare"),
        ("deadlines", "calculate"), ("deadlines", "matrix"), ("deadlines", "anchor"),
        ("deadlines", "confirm"), ("deadlines", "waive"), ("deadlines", "list"),
        ("deadlines", "audit"), ("deadlines", "autofill"),
        ("case", "new"), ("case", "set"), ("case", "list"), ("case", "card"),
        ("case", "amount", "add"), ("case", "doc", "add"), ("case", "note", "add"),
        ("conflict", "check"), ("jurisdiction", "check"),
        ("pretrial", "calc"), ("pretrial", "consequences"),
        ("calendar", "agenda"), ("calendar", "ics"),
        ("reminders", "due"), ("case-search", "alerts"),
        ("case-search", "case-diff"),
    }
    exposed = mcp_server.mcp_exposed_commands()
    missing = REQUIRED_OFFLINE - exposed
    assert not missing, f"не экспонированы: {sorted(missing)}"
    tree = _parser_tree(__import__("sud_cli.cli", fromlist=["build_parser"]).build_parser())
    for path in exposed:
        assert path in tree, f"тул ссылается на несуществующую CLI-команду {path}"


def test_live_network_tools_not_added_by_wave(state_env):
    """Сеть запрещена: новый волновой тул не может быть live-тулом."""
    assert not (W6A_NEW_TOOLS & mcp_server.LIVE_GATED_TOOLS)
    for t in mcp_server.TOOLS:
        if t["name"] in W6A_NEW_TOOLS:
            schema_props = set(t["schema"]["properties"])
            assert "url" not in schema_props and "host" not in schema_props


# ------------------------------------------------- новые тулы: сквозные проверки --

def test_reminders_due_tool_and_days_validation(state_env):
    resp = _call("sud_cli_reminders_due", {"days": 0})
    assert resp["result"]["isError"] is True
    body = _body(_call("sud_cli_case_new", {**_case_args("A110-7/2026"), "confirm": True}))
    case_id = body["case_id"]
    body = _body(_call("sud_cli_deadlines_anchor", {
        "case_id": case_id, "key": "soyu_court_order_cancel",
        "event_date": "2026-10-20", "method": "почта",
        "evidence": "paritet", "confirm": True}))
    assert body["state"] == "computed"
    out = _body(_call("sud_cli_reminders_due", {"days": 30}))
    assert len(out["commands"]) == len(out["items"])
    assert out["bus_free"], "bus-free контракт как у reminders (the wave)"


def test_deadlines_confirm_waive_roundtrip_via_mcp(state_env):
    body = _body(_call("sud_cli_case_new", {**_case_args("A110-8/2026"), "confirm": True}))
    case_id = body["case_id"]
    _body(_call("sud_cli_deadlines_anchor", {
        "case_id": case_id, "key": "as_court_order_cancel",
        "event_date": "2026-10-19", "method": "почта", "evidence": "конверт",
        "confirm": True}))
    confirmed = _body(_call("sud_cli_deadlines_confirm", {
        "case_id": case_id, "key": "as_court_order_cancel", "by": "оператор",
        "confirm": True}))
    assert confirmed["state"] == "confirmed"
    waived = _body(_call("sud_cli_deadlines_waive", {
        "case_id": case_id, "key": "as_court_order_cancel", "by": "оператор",
        "norm": "ч. 4 ст. 229.5 АПК РФ", "confirm": True}))
    assert waived["state"] == "waived"
    # без confirm — waive не производится
    denied = _body(_call("sud_cli_deadlines_waive", {
        "case_id": case_id, "key": "as_court_order_cancel", "by": "х",
        "norm": "ч. 4 ст. 229.5 АПК РФ"}))
    assert denied["status"] == "needs_confirmation"
    audit = _body(_call("sud_cli_deadlines_audit", {"case_id": case_id}))
    assert [e["action"] for e in audit] == ["anchor", "confirm", "waive"]


def test_autofill_tool_respects_write_gate(state_env):
    body = _body(_call("sud_cli_case_new", {**_case_args("A110-9/2026"), "confirm": True}))
    case_id = body["case_id"]
    from sud_cli.db import get_connection
    conn = get_connection(state_env)
    conn.execute(
        "INSERT INTO events (event_id, case_id, event_date, event_type, doc_text,"
        " source_name, source_record_id, imported_at) VALUES (?,?,?,?,?,?,?,?)",
        ("ev-gate", case_id, "2026-09-01", "определение",
         "Определение изготовлено в окончательной форме", "test", "t", "2026-09-01T00:00:00Z"))
    conn.commit()
    conn.close()
    denied = _body(_call("sud_cli_deadlines_autofill", {"case_id": case_id}))
    assert denied["status"] == "needs_confirmation"
    assert _body(_call("sud_cli_deadlines_list", {"case_id": case_id})) == []
    done = _body(_call("sud_cli_deadlines_autofill", {"case_id": case_id, "confirm": True}))
    assert done["created"], done
    assert all(r["state"] == "computed" for r in done["created"])


def test_calendar_tools_via_mcp(state_env):
    out = _body(_call("sud_cli_calendar_agenda", {"days": 14}))
    assert out["empty"] is True
    out = _body(_call("sud_cli_calendar_ics", {"days": 60}))
    assert out["count"] == 0
    assert out["ics"].startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in out["ics"]


def test_case_search_alerts_tool_empty_and_days(state_env, tmp_path):
    out = _body(_call("sud_cli_case_search_alerts",
                      {"days": 7, "alerts_dir": str(tmp_path / "none")}))
    assert out["alerts"] == [] and out["commands"] == []
    assert out["bus_free"]
    err = _call("sud_cli_case_search_alerts", {"days": 91})
    assert err["result"]["isError"] is True
