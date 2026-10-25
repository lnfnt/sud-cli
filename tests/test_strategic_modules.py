import io
import json
from datetime import date

from sud_cli.limitation import calculate_limitation
from sud_cli.mcp_server import handle_request, serve_stdio
from sud_cli.npa_as_of import lookup_as_of
from sud_cli.timeline_audit import audit_timeline


def test_limitation_expiry_notice_and_status():
    result = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                                  claim_notice_days=30, check_date=date(2024, 2, 1))
    assert result.status == "ok"
    assert result.is_expired
    assert result.days_overdue > 0
    assert "ст. 202 ГК РФ" in result.legal_basis[0]


def test_limitation_custom_months_and_working_day_shift():
    result = calculate_limitation(start=date(2026, 1, 31), period_type="custom_months",
                                  custom_months=1, check_date=date(2026, 2, 28))
    assert result.expiry_date == "2026-03-02"
    assert result.days_left == 2


def test_timeline_audit_detects_all_core_anomalies():
    events = [
        {"id": "vud", "timestamp": "2024-01-03T12:00", "action": "ВУД"},
        {"id": "search", "timestamp": "2024-01-02T12:00", "action": "обыск"},
        {"id": "delivery", "timestamp": "2024-01-04T10:00", "action": "доставлен", "participant": "A"},
        {"id": "protocol", "timestamp": "2024-01-04T14:01", "action": "протокол задержания", "participant": "A"},
        {"id": "w1", "timestamp": "2024-01-05T10:00", "action": "обыск", "witnesses": ["W"]},
        {"id": "w2", "timestamp": "2024-01-06T10:00", "action": "выемка", "witnesses": ["W"]},
        {"id": "e1", "timestamp": "2024-01-07T10:00", "evidence_id": "E", "custodian": "X"},
        {"id": "e2", "timestamp": "2024-01-08T10:00", "evidence_id": "E", "custodian": "Y"},
    ]
    result = audit_timeline(events)
    assert {item["code"] for item in result["anomalies"]} == {
        "PRE_INITIATION_ACTION", "DETENTION_PROTOCOL_DELAY",
        "REPEATED_OR_BIASED_WITNESS", "CHAIN_OF_CUSTODY_BROKEN",
    }


def test_npa_as_of_returns_historical_record():
    result = lookup_as_of("gk_rf", "196", "2015-11-20")
    assert result["status"] == "ok"
    assert result["effective_date"] <= "2015-11-20"
    assert result["federal_law"]


def test_mcp_lists_tools_and_serves_json_rpc():
    listed = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    # v2.11.0 W6a §1: +10 офлайн-утилит паритета (35 -> 45), обоснование в
    # tests/test_v2110_1_parity.py::test_tool_count_pin_v2110
    # v2.12.0 the wave §4: +1 read-тул sud_cli_form_new -> 46
    # v2.13.0 история §5: +12 (портфель/данные/ПДн) -> 58; список волны —
    # tests/test_v2110_1_parity.py::W6B2_NEW_TOOLS
    # v2.17.0: +1 sud_cli_license_info; v2.18.0 W7: +2
    assert len(listed["result"]["tools"]) == 65
    source = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "sud_cli_limitation", "arguments": {
            "start": "2020-01-01", "type": "general", "check_date": "2020-02-01"}}}) + "\n")
    output = io.StringIO()
    serve_stdio(source, output)
    response = json.loads(output.getvalue())
    assert response["result"]["isError"] is False
