import json
import os
from sud_cli import mcp_server
from tests.conftest import run_cli


def J(out):
    return json.loads(out)


def E(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def test_jur_apk_general(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "general"], env=e)
    assert code == 0, err
    d = J(out)["data"]
    assert d["candidates"] and d["candidates"][0]["venue_basis"] == "ч.1 ст.35 АПК РФ"
    assert d["candidates"][0]["confidence"] == "hard"
    assert d["candidates"][0].get("fetch_hint") == "npa_as_of" or "legal_text" in d["candidates"][0]


def test_jur_apk_immovable(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--immovable"], env=e)
    assert code == 0
    d = J(out)["data"]
    norms = [c["venue_basis"] for c in d["candidates"]]
    assert "ст.38 АПК РФ" in " ".join(norms)


def test_jur_apk_corporate(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--corporate"], env=e)
    d = J(out)["data"]
    assert any("38" in c["venue_basis"] for c in d["candidates"])


def test_jur_apk_bankruptcy(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--bankruptcy"], env=e)
    d = J(out)["data"]
    assert any("банкрот" in (c.get("court_level","")+c["venue_basis"]) or "ФЗ-127" in c["venue_basis"] for c in d["candidates"])


def test_jur_apk_ip(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--ip-rights"], env=e)
    d = J(out)["data"]
    assert any("СИП" in c["court_level"] or "интеллект" in c["venue_basis"].lower() or "СИП" in c["venue_basis"] for c in d["candidates"])


def test_jur_apk_contract_forum_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--contract-forum"], env=e)
    d = J(out)["data"]
    assert any("37" in m["norm"] for m in d["manual_review"])


def test_jur_apk_arbitration_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--arbitration-clause"], env=e)
    d = J(out)["data"]
    assert any("148" in m["norm"] for m in d["manual_review"])


def test_jur_apk_multiple_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--multiple-defendants"], env=e)
    d = J(out)["data"]
    assert any("36" in m["norm"] for m in d["manual_review"])


def test_jur_apk_foreign_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--foreign-element"], env=e)
    d = J(out)["data"]
    assert len(d["manual_review"]) >= 1


def test_jur_apk_unknown_location(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--unknown-rf-location"], env=e)
    d = J(out)["data"]
    assert any("36" in c["venue_basis"] for c in d["candidates"])


def test_jur_gpk_general(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "general"], env=e)
    d = J(out)["data"]
    assert d["candidates"][0]["venue_basis"] == "ст.28 ГПК РФ"


def test_jur_gpk_consumer(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "x", "--consumer"], env=e)
    d = J(out)["data"]
    assert any("29" in c["venue_basis"] for c in d["candidates"])


def test_jur_gpk_alimony(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "x", "--alimony"], env=e)
    d = J(out)["data"]
    assert any("29" in c["venue_basis"] for c in d["candidates"])


def test_jur_gpk_immovable(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "x", "--immovable"], env=e)
    d = J(out)["data"]
    assert any("30" in c["venue_basis"] for c in d["candidates"])


def test_jur_gpk_contract_forum_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "x", "--contract-forum"], env=e)
    d = J(out)["data"]
    assert any("32" in m["norm"] for m in d["manual_review"])


def test_jur_gpk_spec_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "spec_military"], env=e)
    d = J(out)["data"]
    assert any("25-27" in m["norm"] or "ФКЗ" in m["norm"] for m in d["manual_review"])


def test_jur_gpk_foreign_mr(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "gpk", "--claim", "x", "--foreign-element"], env=e)
    d = J(out)["data"]
    assert any("подраздел V" in m["norm"] or "ГПК" in m["norm"] for m in d["manual_review"])


def test_jur_kas_general(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "kas", "--claim", "general"], env=e)
    d = J(out)["data"]
    assert d["candidates"][0]["venue_basis"] == "ст.22 КАС РФ"


def test_jur_kas_choice(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "kas", "--claim", "x", "--unknown-rf-location"], env=e)
    d = J(out)["data"]
    assert any("24" in c["venue_basis"] for c in d["candidates"])


def test_jur_kas_vs_first(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "kas", "--claim", "vs_first"], env=e)
    d = J(out)["data"]
    assert any("21" in c["venue_basis"] for c in d["candidates"])


def test_jur_empty_combo(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "kas", "--claim", "nothing"], env=e)
    d = J(out)["data"]
    assert d["candidates"] and d["manual_review"] == []


def test_jur_disclaimer(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x"], env=e)
    d = J(out)["data"]
    assert "disclaimer" in d and d["disclaimer"]
    assert d["warnings"]


def test_jur_reference_only_fetch_hint(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "jurisdiction", "check", "--code", "apk", "--claim", "x", "--immovable"], env=e)
    d = J(out)["data"]
    c = [x for x in d["candidates"] if "38" in x["venue_basis"]][0]
    assert c.get("fetch_hint") == "npa_as_of" or "legal_text" in c
    if c.get("fetch_hint"):
        assert "legal_text" not in c


def test_pretrial_apk_30(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "pretrial", "calc", "--code", "apk", "--sent", "2026-10-01"], env=e)
    d = J(out)["data"]
    assert d["days"] == 30 and d["response_deadline"] == "2026-10-31"


def test_pretrial_zozpp_10(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "pretrial", "calc", "--code", "zozpp", "--sent", "2026-10-01"], env=e)
    d = J(out)["data"]
    assert d["days"] == 10 and d["response_deadline"] == "2026-10-11"


def test_pretrial_osago_shift(tmp_path):
    e = E(tmp_path)
    code, out, err = run_cli(["--json", "pretrial", "calc", "--code", "osago", "--sent", "2026-10-01"], env=e)
    d = J(out)["data"]
    assert d["exclude_nonworking_holidays"] is True
    assert d["response_deadline"] == "2026-11-02"


def test_pretrial_charter_hard_and_postal_mr(tmp_path):
    """Обновлено 24.10: legal проверено заменила NOT_SERIALIZED-заглушку
    VERIFIED нормами уставов — railway 30d hard, postal manual_review (30/5)."""
    e = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "pretrial", "calc", "--code", "railway",
                              "--sent", "2026-10-01"], env=e)
    assert code == 0, err
    d = json.loads(out)["data"]
    assert d["days"] == 30 and "120" in str(d.get("norm"))
    code, out, err = run_cli(["--json", "pretrial", "calc", "--code", "postal",
                              "--sent", "2026-10-01"], env=e)
    assert code == 0, err
    d = json.loads(out)["data"]
    assert any(m.get("reason") == "manual_review" for m in d.get("manual_review", [])), d

def test_pretrial_consequences_all(tmp_path):
    e = E(tmp_path)
    for code in ["apk", "gpk", "kas"]:
        for stage in ["filing", "trial", "movement"]:
            code_, out, err = run_cli(["--json", "pretrial", "consequences", "--code", code, "--stage", stage], env=e)
            assert code_ == 0, err
            d = J(out)["data"]
            assert d["code"] == code and d["stage"] == stage


def test_mcp_schema_jurisdiction(tmp_path):
    tools = mcp_server.tool_list()["tools"]
    for name in ["sud_cli_jurisdiction_check", "sud_cli_pretrial_calc", "sud_cli_pretrial_consequences"]:
        t = next(x for x in tools if x["name"] == name)
        assert t["inputSchema"].get("additionalProperties") is False


def test_mcp_no_duplicates():
    tools = mcp_server.tool_list()["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))


def test_mcp_registration_count():
    tools = mcp_server.tool_list()["tools"]
    # v2.11.0 W6a §1: 35 -> 45, ровно +10 офлайн-утилит паритета
    # v2.12.0 the wave §4: 45 -> 46, +1 read-тул sud_cli_form_new
    # v2.13.0 история §5: 46 -> 58, +12 тулов волны портфеля/данных/ПДн
    # v2.14.0 S-03: 58 -> 59 egrul; v2.15.0 S-61: 59 -> 62, +3 fssp
    # v2.17.0: 62 -> 63, +1 sud_cli_license_info (§13)
    # v2.18.0 W7: 63 -> 65, +1 efrsb_link +1 plenum_search
    assert len(tools) == 65


def test_mcp_call_jurisdiction(tmp_path):
    os.environ["SUD_CLI_STATE_DIR"] = str(tmp_path)
    try:
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sud_cli_jurisdiction_check", "arguments": {"code": "apk", "claim": "x", "immovable": True}}})
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["isError"] is False
        assert body["candidates"]
    finally:
        os.environ.pop("SUD_CLI_STATE_DIR", None)


def test_mcp_call_pretrial(tmp_path):
    os.environ["SUD_CLI_STATE_DIR"] = str(tmp_path)
    try:
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sud_cli_pretrial_calc", "arguments": {"code": "apk", "sent": "2026-10-01"}}})
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["isError"] is False
        assert body["days"] == 30
        resp2 = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "sud_cli_pretrial_consequences", "arguments": {"code": "gpk", "stage": "filing"}}})
        body2 = json.loads(resp2["result"]["content"][0]["text"])
        assert body2["norm"] == "п.1 ч.1 ст.135 ГПК РФ"
    finally:
        os.environ.pop("SUD_CLI_STATE_DIR", None)
