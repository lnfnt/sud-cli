"""v2.3.3 — регресс внешнего ревью (review). Каждое — подтверждённый finding."""
import io
import json
import sys
from datetime import date
from decimal import Decimal

from sud_cli import mcp_server


def _call(name, args):
    return mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})


def test_gate_zero_string_blocks(monkeypatch):
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "0")  # раньше "0" = truthy = OPEN
    res = _call("sud_cli_case_history", {"case_number": "А40-1-2025"})
    assert json.loads(res["result"]["content"][0]["text"])["status"] == "blocked"
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "off")
    res = _call("sud_cli_practice_search", {"query": "x"})
    assert json.loads(res["result"]["content"][0]["text"])["status"] == "blocked"


def test_gate_on_enables(monkeypatch):
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "on")
    import sud_cli.case_search.public_api as api
    monkeypatch.setattr(api, "search_practice", lambda **kw: {"status": "ok", "marker": True})
    res = _call("sud_cli_practice_search", {"query": "x"})
    assert "marker" in res["result"]["content"][0]["text"]


def test_mcp_fees_rejects_unsupported_class():
    res = _call("sud_cli_fees", {"jurisdiction": "arbitration", "fee_kind": "property_claim",
                                 "claim_amount": 100000, "claim_class": "criminal"})
    text = res["result"]["content"][0]["text"]
    assert "UNSUPPORTED" in text.upper() or "criminal" in text
    good = _call("sud_cli_fees", {"jurisdiction": "arbitration", "fee_kind": "property_claim",
                                  "claim_amount": 100000, "claim_class": "economic"})
    d = json.loads(good["result"]["content"][0]["text"])
    assert d.get("status") == "ok"


def test_interest_year_boundary_segments():
    from sud_cli.interest import calculate_interest
    r = calculate_interest(Decimal("1000000"), date(2025, 12, 20), date(2026, 1, 10), "395", None).to_dict()
    periods = r["periods"]
    assert any(p["to"] == "2026-01-01" for p in periods), periods
    # 2025 — невисокосный: все декабрьские дни (с учётом rate-границы 22.12) = 12 по 365
    dec = [q for q in r["periods"] if q["from"].startswith("2025")]
    assert dec and sum(q["days"] for q in dec) == 12


def test_stdio_survives_bad_frame():
    inp = io.StringIO("{bad json\n")
    out = io.StringIO()
    mcp_server.serve_stdio(input_stream=inp, output_stream=out)
    assert '"-32700"' in out.getvalue().replace(" ", "") or "Parse error" in out.getvalue()


def test_efrsb_error_excludes_query(tmp_path, monkeypatch):
    import urllib.error as ue
    import urllib.request as ur
    import sud_cli.efrsb_public as E
    def fake_open(self, req, timeout=None):
        raise ue.HTTPError("https://bankrot.fedresurs.ru/companies?searchString=Иванов%20ИИ",
                           403, "Forbidden", {}, None)
    monkeypatch.setattr(ur.OpenerDirector, "open", fake_open)
    try:
        E.search_companies(name="Иванов ИИ")
        assert False, "ожидался EfrsbPublicError"
    except E.EfrsbPublicError as exc:
        assert "Иванов" not in str(exc) and "searchString" not in str(exc)


def test_efrsb_params_exclusive():
    import sud_cli.efrsb_public as E
    try:
        E.search_companies(inn="7700000000", name="Ромашка")
        assert False
    except E.EfrsbPublicError as exc:
        assert "взаимоисключающ" in str(exc)
