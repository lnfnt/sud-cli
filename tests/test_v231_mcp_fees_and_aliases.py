"""v2.3.1 Фикс 2 (ТЗ task-brief): MCP vs CLI — имена и типы fees + алиасы.

Факт из ТЗ: MCP `sud_cli_fees` требовал `fee_kind`, тогда как CLI знает только
`--kind`; `claim_amount` был объявлен `integer` — копейки падали и там.
Обратная совместимость: существующие вызовы с `fee_kind` не ломаются.

Аудит 19 инструментов зафиксирован как дрейф-детектор:
``test_cli_dests_reachable_from_mcp_schema`` — каждый dest argparse обязан быть
либо свойством схемы MCP, либо в явно задокументированном списке исключений.
"""
import json

import pytest

from sud_cli import mcp_server

FEE_KINDS = ["property_claim", "court_order", "non_property_claim", "appeal",
             "cassation_first", "cassation_supreme", "bankruptcy_petition",
             "bankruptcy_creditor_claim"]


def _call(name, arguments, request_id=11):
    return mcp_server.handle_request({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def _schema(name):
    return next(t for t in mcp_server.TOOLS if t["name"] == name)["schema"]


# ------------------------------------------------------- схема: claim_amount


def test_fees_schema_declares_claim_amount_as_number():
    """Acceptance-строка ТЗ: tools/list показывает claim_amount number."""
    listed = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list"})
    fees = next(t for t in listed["result"]["tools"] if t["name"] == "sud_cli_fees")
    assert fees["inputSchema"]["properties"]["claim_amount"]["type"] == "number"


def test_fees_schema_declares_both_kind_and_fee_kind():
    props = _schema("sud_cli_fees")["properties"]
    assert "fee_kind" in props and "kind" in props
    assert props["kind"]["enum"] == FEE_KINDS
    assert props["fee_kind"]["enum"] == FEE_KINDS


def test_fees_required_accepts_either_name():
    schema = _schema("sud_cli_fees")
    assert "fee_kind" not in schema.get("required", [])
    alternatives = schema["anyOf"]
    assert {"required": ["fee_kind"]} in alternatives
    assert {"required": ["kind"]} in alternatives


def test_fees_schema_keeps_additional_properties_closed():
    assert _schema("sud_cli_fees")["additionalProperties"] is False


def test_fees_schema_jurisdiction_enum_matches_the_engine():
    """Раньше enum рекламировал arbitral/soyu/constitutional — движок знает
    только arbitration/general_jurisdiction/kas (ключи legal_rules.json)."""
    props = _schema("sud_cli_fees")["properties"]
    assert set(props["jurisdiction"]["enum"]) == {"arbitration", "general_jurisdiction", "kas"}


# ------------------------------------------------------- вызов: fees через MCP


def test_mcp_fees_fee_kind_still_works_backwards_compatible():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "fee_kind": "property_claim", "claim_amount": 3697453})
    assert resp["result"]["isError"] is False, resp
    assert _body(resp)["amount_rub"] == 49882


def test_mcp_fees_kind_alias_works():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "kind": "property_claim", "claim_amount": 3697453})
    assert resp["result"]["isError"] is False, resp
    assert _body(resp)["amount_rub"] == 49882


def test_mcp_fees_accepts_kopeeks():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "fee_kind": "property_claim", "claim_amount": 3697453.16})
    assert resp["result"]["isError"] is False, resp
    body = _body(resp)
    assert body["status"] == "ok" and body["amount_rub"] == 49882


def test_mcp_fees_kopeeks_survive_as_string_and_decimal():
    """float из JSON → Decimal(str) — иначе бинарный float тихо меняет копейки."""
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "kind": "property_claim", "claim_amount": "4000071.43"})
    assert resp["result"]["isError"] is False, resp
    assert _body(resp)["amount_rub"] == 52001


def test_mcp_fees_conflicting_kind_and_fee_kind_is_rejected():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "kind": "appeal", "fee_kind": "property_claim",
                                  "claim_amount": 1000})
    assert resp["result"]["isError"] is True
    assert "kind" in resp["result"]["content"][0]["text"]


def test_mcp_fees_agreeing_kind_and_fee_kind_is_accepted():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "kind": "property_claim", "fee_kind": "property_claim",
                                  "claim_amount": 3697453.16})
    assert resp["result"]["isError"] is False, resp
    assert _body(resp)["amount_rub"] == 49882


def test_mcp_fees_requires_some_kind_name():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "claim_amount": 1000})
    assert resp["result"]["isError"] is True
    assert "kind" in resp["result"]["content"][0]["text"]


def test_mcp_fees_applicant_alias_still_works():
    """CLI dest --applicant уже работал; проверка, что фикс его не сломал."""
    resp = _call("sud_cli_fees", {"jurisdiction": "arbitration", "kind": "appeal",
                                  "applicant": "entity"})
    assert resp["result"]["isError"] is False, resp
    assert _body(resp)["amount_rub"] == 30000


def test_mcp_fees_applicant_conflict_is_rejected():
    resp = _call("sud_cli_fees", {"jurisdiction": "arbitration", "fee_kind": "appeal",
                                  "applicant": "entity", "applicant_type": "individual"})
    assert resp["result"]["isError"] is True


def test_mcp_fees_jurisdiction_aliases():
    """arbitral/soyu из старого enum не должны ронять вызов в manual review."""
    for juris, expected in (("arbitral", 30000), ("soyu", 15000), ("general", 15000)):
        resp = _call("sud_cli_fees", {"jurisdiction": juris, "kind": "appeal",
                                      "applicant": "entity"})
        assert resp["result"]["isError"] is False, (juris, resp)
        assert _body(resp)["amount_rub"] == expected, juris


def test_mcp_fees_rejects_bool_amount():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "fee_kind": "property_claim", "claim_amount": True})
    assert resp["result"]["isError"] is True


def test_mcp_fees_unknown_kind_is_typed_error_not_crash():
    resp = _call("sud_cli_fees", {"jurisdiction": "general_jurisdiction",
                                  "kind": "nonsense"})
    assert resp["result"]["isError"] is False  # движок отвечает статусом, а не исключениями
    assert _body(resp)["status"] == "MANUAL_LEGAL_REVIEW_REQUIRED"


# --------------------------------------------------- точечные алиасы (аудит)


def test_mcp_limitation_accepts_cli_years_months():
    """CLI dest --years/--months = движковые custom_years/custom_months."""
    from datetime import date

    from sud_cli.limitation import calculate_limitation
    via_cli_names = _call("sud_cli_limitation", {"start": "2020-01-01",
                                                 "period_type": "custom_years",
                                                 "years": 2, "months": 3,
                                                 "check_date": "2021-01-01"})
    assert via_cli_names["result"]["isError"] is False, via_cli_names
    canonical = calculate_limitation(start=date(2020, 1, 1), period_type="custom_years",
                                     custom_years=2, custom_months=3,
                                     check_date=date(2021, 1, 1)).to_dict()
    assert _body(via_cli_names) == canonical


def test_mcp_limitation_alias_conflict_is_rejected():
    resp = _call("sud_cli_limitation", {"start": "2020-01-01", "years": 1,
                                        "custom_years": 3})
    assert resp["result"]["isError"] is True


def test_mcp_case_history_accepts_cli_case_id(monkeypatch):
    # hermetic (D6-контроль 24.10): алиас-путь живёт за bridge-гейтом — не зависеть от ambient env
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "1")
    seen = {}

    def fake_history(case_number=None, *, section="arbitral", max_related=5, limit=10, **kw):
        seen["case_number"] = case_number
        return {"status": "ok", "case_number": case_number}

    from sud_cli.case_search import public_api
    monkeypatch.setattr(public_api, "case_history", fake_history)
    resp = _call("sud_cli_case_history", {"case_id": "А40-12345-2026"})
    assert resp["result"]["isError"] is False, resp
    assert seen["case_number"] == "А40-12345-2026"


def test_mcp_case_history_keeps_case_number(monkeypatch):
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "1")
    seen = {}

    def fake_history(case_number=None, **kw):
        seen["case_number"] = case_number
        return {"status": "ok"}

    from sud_cli.case_search import public_api
    monkeypatch.setattr(public_api, "case_history", fake_history)
    resp = _call("sud_cli_case_history", {"case_number": "А40-1-2026"})
    assert resp["result"]["isError"] is False, resp
    assert seen["case_number"] == "А40-1-2026"


def test_mcp_efrsb_persons_accepts_cli_name(monkeypatch):
    seen = {}

    def fake_persons(full_name=None, birth_date=None, limit=5):
        seen["full_name"] = full_name
        return {"status": "ok"}

    from sud_cli import efrsb_public
    monkeypatch.setattr(efrsb_public, "search_persons", fake_persons)
    resp = _call("sud_cli_efrsb_persons", {"name": "Иванов Иван Иванович"})
    assert resp["result"]["isError"] is False, resp
    assert seen["full_name"] == "Иванов Иван Иванович"


def test_mcp_interest_accepts_cli_date_dests(monkeypatch):
    """CLI dest'ы start_date/end_date уже принимались хендлером, но не были
    объявлены в схеме — агент, валидирующий вызов по схеме, их бы отбросил."""
    props = _schema("sud_cli_interest")["properties"]
    assert {"start_date", "end_date"} <= set(props)
    resp = _call("sud_cli_interest", {"amount": 100000, "start_date": "2024-01-01",
                                      "end_date": "2024-02-01"})
    assert resp["result"]["isError"] is False, resp
    assert _body(resp)["status"] == "ok"


def test_mcp_section_enums_are_values_the_backend_accepts():
    """В схеме были 'general' — normalize_section его не знает (ValueError)."""
    from sud_cli.case_search.sudact.search import SECTION_ALIASES
    for tool in ("sud_cli_practice_search", "sud_cli_case_history"):
        enum = _schema(tool)["properties"]["section"]["enum"]
        assert "general" not in enum, tool
        assert set(enum) <= set(SECTION_ALIASES), tool


def test_mcp_timeline_events_description_matches_the_engine():
    """Движок читает event['timestamp']; описание схемы обещало date|time."""
    desc = _schema("sud_cli_timeline_audit")["properties"]["events"]["description"]
    assert "timestamp" in desc


# ------------------------------------------------------- серверный контракт


def test_initialize_reports_v231():
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    from sud_cli import __version__
    assert resp["result"]["serverInfo"]["version"] == __version__  # v2.3.2: не цементировать патч-версию тестом


# --------------------------------------------------------- дрейф-детектор


TOOL_CLI_PATH = {
    "sud_cli_limitation": ["limitation"], "sud_cli_deadlines": ["deadlines"],
    "sud_cli_fees": ["fees"], "sud_cli_interest": ["interest"],
    "sud_cli_practice_search": ["practice"], "sud_cli_kad_search": ["kad"],
    "sud_cli_timeline_audit": ["timeline-audit"], "sud_cli_npa_as_of": ["npa-as-of"],
    "sud_cli_case_history": ["case-history"], "sud_cli_payment": ["payment"],
    "sud_cli_deferral": ["deferral"],
    "sud_cli_efrsb_companies": ["efrsb", "companies"],
    "sud_cli_efrsb_persons": ["efrsb", "persons"],
    "sud_cli_efrsb_publications": ["efrsb", "publications"],
    "sud_cli_efrsb_managers": ["efrsb", "managers"],
    "sud_cli_sudrf_live": ["sudrf-live"], "sud_cli_mos_live": ["mos-live"],
    "sud_cli_case_diff": ["case-search", "case-diff"],
    "sud_cli_npa_compare": ["npa", "compare"],
}
GLOBAL_DESTS = {"json_mode", "state_dir", "version", "func", "help", "command"}

# Свойства схемы без прямого CLI-аналога: имена движка или задокументированные
# алиасы снапшотов (v2.3.0).
MCP_ONLY_OK = {
    "sud_cli_limitation": {"custom_years", "custom_months"},
    "sud_cli_fees": {"fee_kind", "applicant_type"},
    "sud_cli_interest": {"start", "end"},
    "sud_cli_case_history": {"case_number"},
    "sud_cli_efrsb_persons": {"full_name"},
    "sud_cli_case_diff": {"old_snapshot", "new_snapshot"},
}
# CLI-параметры, которые MCP сознательно не раскрывает (результат аудита).
CLI_NOT_EXPOSED = {
    "sud_cli_sudrf_live": {"host"},
    "sud_cli_interest": {"calc_fee", "jurisdiction", "kind", "applicant"},
}


def _cli_names(parser_path):
    """(dest'ы, имена длинных опций) подкоманды: --type→dest period_type."""
    from sud_cli.cli import build_parser

    current = build_parser()
    for token in parser_path:
        subs = [a for a in current._actions if isinstance(getattr(a, "choices", None), dict)]
        assert subs, f"нет подкоманды {token}"
        current = subs[0].choices[token]
    actions = [a for a in current._actions if a.dest not in GLOBAL_DESTS]
    dests = {a.dest for a in actions}
    flag_names = set()
    for a in actions:
        for opt in a.option_strings:
            if opt.startswith("--"):
                flag_names.add(opt.lstrip("-").replace("-", "_"))
    # имя флага, которое НЕ равно dest'у — это и есть CLI-алиас (агент копирует
    # флаги из --help, а не dest'ы argparse)
    return dests, flag_names - dests


@pytest.mark.parametrize("tool", sorted(TOOL_CLI_PATH))
def test_cli_dests_reachable_from_mcp_schema(tool):
    """Каждый CLI dest должен вызываться через MCP (напрямую или алиасом)."""
    schema = _schema(tool)
    props = set(schema["properties"])
    cli_dests, cli_alias_names = _cli_names(TOOL_CLI_PATH[tool])
    unreachable = cli_dests - props - CLI_NOT_EXPOSED.get(tool, set())
    assert not unreachable, f"{tool}: CLI-имена недоступны в MCP: {sorted(unreachable)}"
    orphan_props = props - cli_dests - cli_alias_names - MCP_ONLY_OK.get(tool, set())
    assert not orphan_props, f"{tool}: свойства схемы без CLI-аналога: {sorted(orphan_props)}"
