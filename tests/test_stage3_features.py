import json
from datetime import date

from tests.conftest import run_cli
from sud_cli.interest import calculate_interest
from sud_cli.legal_engine import STATUS_MISSING_PARAM, STATUS_OK, calculate_fee, load_rules


def test_partial_payments_split_periods_and_reduce_balance():
    result = calculate_interest(
        100_000, date(2026, 1, 1), date(2026, 5, 1),
        payments="2026-02-15:20000,2026-04-10:30000",
    )
    assert result.status == STATUS_OK
    assert result.remaining_principal == 50_000
    assert [period["principal_rub"] for period in result.periods] == [100_000, 80_000, 80_000, 50_000]
    assert result.to_dict()["remaining_principal_rub"] == 50_000.0


def test_partial_payments_accept_json_and_reject_overpayment():
    result = calculate_interest(
        100_000, date(2026, 1, 1), date(2026, 3, 1),
        payments=json.dumps([{"date": "2026-02-01", "amount": 25_000}]),
    )
    assert result.remaining_principal == 75_000
    assert calculate_interest(100_000, date(2026, 1, 1), date(2026, 3, 1), "395", "2026-02-01:100001").status == STATUS_MISSING_PARAM


def test_bankruptcy_petition_fees_and_required_parameters():
    rules = load_rules()
    assert calculate_fee(jurisdiction="arbitration", fee_kind="bankruptcy_petition", applicant_type="entity", rules=rules).amount == 100_000
    assert calculate_fee(jurisdiction="arbitration", fee_kind="bankruptcy_petition", applicant_type="individual", rules=rules).amount == 10_000
    assert calculate_fee(jurisdiction="arbitration", fee_kind="bankruptcy_petition", rules=rules).status == STATUS_MISSING_PARAM
    assert calculate_fee(jurisdiction="arbitration", fee_kind="bankruptcy_creditor_claim", rules=rules).status == STATUS_MISSING_PARAM


def test_bankruptcy_creditor_claim_uses_property_scale():
    # По пп. 9 п. 1 ст. 333.21 НК РФ (в ред. 259-ФЗ): 50% ставки имущественного иска.
    # Для 500 000 руб.: ставка иска = 10 000 + 5% от 400 000 = 30 000 руб.
    # 50% от 30 000 руб. = 15 000 руб.
    result = calculate_fee(
        claim_amount=500_000, jurisdiction="arbitration",
        fee_kind="bankruptcy_creditor_claim", rules=load_rules(),
    )
    assert result.status == STATUS_OK
    assert result.amount == 15_000


def test_json_flag_is_valid_at_command_end():
    code, output, _ = run_cli([
        "fees", "--jurisdiction", "arbitration", "--kind", "bankruptcy_petition",
        "--applicant", "entity", "--json",
    ])
    assert code == 0
    assert json.loads(output)["data"]["amount_rub"] == 100_000


def test_json_flag_is_valid_in_command_middle():
    code, output, _ = run_cli(["fees", "--json", "--jurisdiction", "arbitration", "--kind", "bankruptcy_petition", "--applicant", "individual"])
    assert code == 0
    assert json.loads(output)["data"]["amount_rub"] == 10_000
