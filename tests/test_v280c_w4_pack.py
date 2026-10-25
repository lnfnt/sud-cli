"""W4c пины: льготы/возврат 333.40/индексация на VERIFIED pack legal (акт 2026.10.24)."""
import json
import subprocess
import sys
from decimal import Decimal

import pytest

from sud_cli.fees_extra import (fee_exemption_check, fee_refund,
                                 indexation_compute, indexation_frame)
from sud_cli.legal_engine import load_rules


@pytest.fixture()
def rules():
    return load_rules()


def test_pack_present_v120(rules):
    assert rules["version"] in ("1.2.0", "1.3.0", "1.4.0", "1.5.0")  # 1.3.0: W4d historical tariffs additive
    pack = rules["fee_exemptions_and_refunds"]
    assert {"exemptions_333_35", "exemptions_333_36", "exemptions_333_37",
            "refund_333_40", "interest_and_indexation"} <= set(pack)


def test_exemption_hit_full(rules):
    r = fee_exemption_check(rules, jurisdiction="apk", category_code="arbitration_disabled_1_2")
    assert r["status"] == "exempt"
    assert "333.37" in r["statute"] and r["basis"].startswith("пп. 2")


def test_exemption_unknown_is_manual_not_guess(rules):
    r = fee_exemption_check(rules, jurisdiction="gpk", category_code="пенсионер_все_подряд")
    assert r["status"] == "manual_review" and r["fetch_hint"] == "npa_as_of"
    assert "не догадка" in r["detail"]


def test_exemption_zozpp_labor(rules):
    r = fee_exemption_check(rules, jurisdiction="gpk", category_code="labor_plaintiff")
    assert r["status"] == "exempt"


def test_refund_shares(rules):
    ok = {g["code"]: Decimal(str(g.get("refund_share", 1)))
          for g in rules["fee_exemptions_and_refunds"]["refund_333_40"]["grounds"]}
    assert ok["overpaid"] == Decimal("1.0")
    assert ok["settlement_first_instance"] == Decimal("0.7")
    assert ok["settlement_appeal"] == Decimal("0.5")
    assert ok["settlement_cassation_supervision"] == Decimal("0.3")


def test_refund_70_exact(rules):
    r = fee_refund(rules, paid=Decimal("50000"), ground="settlement_first_instance")
    assert r["status"] == "ok" and r["refund_amount"] == "35000.00"


def test_refund_claim_admitted_zeroes_settlement(rules):
    r = fee_refund(rules, paid=Decimal("50000"), ground="settlement_first_instance",
                   claim_admitted=True)
    assert r["refund_amount"] == "0.00"
    assert any("признание иска" in n for n in r["conditions"])


def test_refund_plaintiff_default_exception(rules):
    r = fee_refund(rules, paid=Decimal("10000"), ground="dismissed_no_consideration",
                   plaintiff_default=True)
    assert r["refund_amount"] == "0.00"


def test_refund_unknown_ground_manual(rules):
    r = fee_refund(rules, paid=Decimal("1"), ground="захотелось")
    assert r["status"] == "manual_review"


def test_indexation_frame_no_data_no_numbers(rules):
    f = indexation_frame(rules, code="apk")
    assert f["status"] == "frame_only" and "ИПЦ" in f["index"]
    c = indexation_compute(rules, code="apk", amount=Decimal("1000"), cpi_start=None, cpi_end=None)
    assert c["status"] == "data_unavailable"


def test_indexation_compute_marks_unverified(rules):
    c = indexation_compute(rules, code="gpk", amount=Decimal("100000"),
                           cpi_start=Decimal("1.0"), cpi_end=Decimal("1.084"))
    assert c["indexed_amount"] == "108400.00"
    assert "unverified" in c["provenance"]


def test_cli_fee_refund_json(tmp_path):
    from tests.conftest import run_cli
    code, out, err = run_cli(["--json", "fee-refund", "--paid", "1000",
                              "--ground", "overpaid"], env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code == 0, err
    d = json.loads(out)["data"]
    assert d["refund_amount"] == "1000.00"


def test_mcp_tools_registered():
    from sud_cli import mcp_server
    names = [t["name"] for t in mcp_server.TOOLS]
    for want in ("sud_cli_fee_exemption", "sud_cli_fee_refund", "sud_cli_fee_indexation"):
        assert want in names
    assert len(names) == len(set(names))
