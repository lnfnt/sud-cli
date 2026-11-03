# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""v2.3.2 — engineering-блок по независимому аудиту sa-1 (передан colleague, A2A)."""
import json
import os
from datetime import date
from decimal import Decimal

from sud_cli import mcp_server
from sud_cli.efrsb_public import EfrsbPublicError, _page_data
from sud_cli.interest import (_RATE_PERIODS_EMBEDDED, RATE_PERIODS,
                              RATES_META, calculate_interest)
from sud_cli.production_calendar import is_working_day


def test_key_rates_json_is_source_of_truth():
    from pathlib import Path
    d = json.loads((Path(mcp_server.__file__).with_name("data") / "key_rates.json").read_text(encoding="utf-8"))
    assert len(d["periods"]) >= 13
    assert RATE_PERIODS == _RATE_PERIODS_EMBEDDED  # фолбэк и файл согласованы
    assert "ЦБ" in RATES_META["provenance"]


def test_interest_coverage_warning_after_latest_rate():
    r = calculate_interest(100000, date(2026, 1, 1), date(2026, 12, 31), "395", None).to_dict()
    assert r["status"] == "ok"
    assert any("extrapolated" in w for w in r.get("rates_warnings", []))
    old = calculate_interest(100000, date(2024, 1, 1), date(2024, 6, 30), "395", None).to_dict()
    assert not old.get("rates_warnings")


def test_st112_derived_transfer_2025():
    # S1-14: постановление № 1335 от 04.10.2024 зашито — авто-переносы
    # ч. 2 ст. 112 перекрыты декретом (auto_transfers_st112). 23.02 и 08.03
    # переносятся Правительством на 08.05 и 13.06, поэтому 24.02 и 10.03
    # рабочие по официальному производственному календарю (сверено).
    assert is_working_day(date(2025, 2, 21)) is True
    assert is_working_day(date(2025, 2, 24)) is True   # перенос перекрыт: выходной ушёл на 08.05
    assert is_working_day(date(2025, 1, 9)) is True    # январский блок не двигаем
    assert is_working_day(date(2025, 3, 10)) is True   # перенос перекрыт: выходной ушёл на 13.06
    assert is_working_day(date(2025, 5, 8)) is False   # перенос с 23.02
    assert is_working_day(date(2025, 6, 13)) is False  # перенос с 08.03


def test_st112_explicit_entries_not_double_shifted():
    assert is_working_day(date(2026, 3, 9)) is False  # явный 08.03->09.03
    assert is_working_day(date(2026, 5, 12)) is True  # 09.05 перенесён на 11.05, не дальше
    assert is_working_day(date(2027, 5, 10)) is False  # явный 09.05.27->10.05


def _call_tool(name, args):
    return mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})


def test_live_gate_blocks_without_env(monkeypatch):
    monkeypatch.delenv("SUD_CLI_BRIDGE_LIVE", raising=False)
    calls = {"sud_cli_practice_search": {"query": "тест"},
             "sud_cli_kad_search": {"name": "ООО Ромашка"},
             "sud_cli_case_history": {"case_number": "А40-1-2025"}}
    for tool, args in calls.items():
        res = _call_tool(tool, args)
        assert res["result"]["isError"] is False, tool
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload["status"] == "blocked", tool


def test_efrsb_domain_error_becomes_iserror_not_crash():
    # sa-1 F-06: пустой запрос -> EfrsbPublicError внутри; handle_request обязан
    # вернуть isError=true, а не убить stdio-цикл.
    res = _call_tool("sud_cli_efrsb_companies", {})
    assert res["result"]["isError"] is True


def test_page_data_null_is_degraded_not_notfound():
    # sa-1 F-07: WAF-обёртка {"pageData": null} маскировалась под «данных нет».
    for payload in ({}, {"pageData": None}):
        try:
            _page_data(payload)
            assert False, "ожидался EfrsbPublicError"
        except EfrsbPublicError:
            pass
    assert _page_data({"pageData": []}) == []  # честная пустота осталась пустотой
