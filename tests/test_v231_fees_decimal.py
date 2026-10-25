"""v2.3.1 Фикс 1 (ТЗ task-brief): цена иска с копейками — Decimal.

Точная строка воспроизведения из ТЗ (боевой аудит colleague-legal, письмо 001a1609),
до фикса падала на argparse:

    sud-cli fees --jurisdiction general_jurisdiction --kind property_claim \
        --claim-amount 3697453.16
    → sud-cli fees: error: argument --claim-amount: invalid int value: '3697453.16'

Юридическая постановка подтверждена colleague-legal (письмо 001a160a6eff, 21.10):
база расчёта — ТОЧНАЯ цена иска с копейками (п. 1 ст. 333.19, ст. 333.21 НК РФ,
ст. 91 ГПК РФ, ст. 103 АПК РФ); округление по правилу 50 копеек (п. 6 ст. 52
НК РФ) применяется строго к ИТОГУ существующим ``_round_rubles``.
"""
import json
from decimal import Decimal

from tests.conftest import run_cli

from sud_cli.legal_engine import (
    STATUS_MANUAL_REVIEW,
    STATUS_MISSING_PARAM,
    STATUS_OK,
    calculate_fee,
    load_rules,
)

GJ = "general_jurisdiction"


def _fees(*claim_amount):
    return run_cli(["--json", "fees", "--jurisdiction", GJ,
                    "--kind", "property_claim", "--claim-amount", *claim_amount])


# --------------------------------------------------------------------- CLI


def test_cli_fees_accepts_claim_amount_with_kopecks():
    """Строка воспроизведения из ТЗ обязана отдавать status ok / 49882."""
    code, out, err = _fees("3697453.16")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["amount_rub"] == 49882


def test_cli_fees_kopecks_and_whole_ruble_agree():
    """3697453.16 и 3697453 дают одинаковую пошлину (49882) — как в ТЗ."""
    code_dec, out_dec, err1 = _fees("3697453.16")
    code_int, out_int, err2 = _fees("3697453")
    assert (code_dec, code_int) == (0, 0), (err1, err2)
    assert json.loads(out_dec)["data"]["amount_rub"] == \
        json.loads(out_int)["data"]["amount_rub"] == 49882


def test_cli_fees_breakdown_shows_exact_kopeck_base():
    """База в breakdown — точная сумма с копейками, а не обрезанный рубль."""
    code, out, err = _fees("3697453.16")
    assert code == 0, err
    steps = [s["step"] for s in json.loads(out)["data"]["breakdown"]]
    assert any("3,697,453.16" in step for step in steps), steps


def test_cli_fees_rounds_only_the_total_not_the_base():
    """Копейки решают исход: 4 000 071.43 → 52 001, а 4 000 071 → 52 000.

    45000 + 0.7% × 1 000 071.43 = 52 000.50001 → 52 001 (округление вверх по
    п. 6 ст. 52 НК РФ). Если бы база предварительно усекалась до рубля, вышло
    бы 52 000.497 → 52 000 — то есть юридически неверный ответ.
    """
    code_up, out_up, err1 = _fees("4000071.43")
    code_flat, out_flat, err2 = _fees("4000071")
    assert (code_up, code_flat) == (0, 0), (err1, err2)
    assert json.loads(out_up)["data"]["amount_rub"] == 52001
    assert json.loads(out_flat)["data"]["amount_rub"] == 52000


def test_cli_fees_drops_sub_50_kopeck_remainder_at_the_total():
    """3 000 049.90 → 45 000.3493 → 45 000: копейки отбрасываются на итоге."""
    code, out, err = _fees("3000049.90")
    assert code == 0, err
    assert json.loads(out)["data"]["amount_rub"] == 45000


def test_cli_fees_rejects_non_numeric_claim_amount():
    """Мусор — ошибка argparse с внятным текстом, а не «invalid int value».

    В текстовом режиме сохранено поведение argparse (exit 2); в --json режиме
    CLI по контракту отдаёт typed INVALID_ARGUMENTS (см. следующий тест).
    """
    code, out, err = run_cli(["fees", "--jurisdiction", GJ, "--kind", "property_claim",
                              "--claim-amount", "abc"])
    assert code == 2
    assert "--claim-amount" in err
    assert "invalid claim amount" in err
    assert "invalid int value" not in err


def test_cli_fees_malformed_amount_keeps_json_error_contract():
    """--json + битые аргументы: по-прежнему typed INVALID_ARGUMENTS, exit 1."""
    code, out, err = _fees("abc")
    assert code == 1
    payload = json.loads(err)
    assert payload["status"] == "error"
    assert payload["data"]["status"] == "INVALID_ARGUMENTS"


def test_cli_fees_rejects_non_finite_amounts():
    """Decimal('NaN')/Decimal('Infinity') валидны как Decimal — и недопустимы
    как цена иска: движок на них молчал бы или сравнивал неверно."""
    for bad in ("NaN", "Infinity", "-Infinity", "sNaN"):
        code, out, err = run_cli(["fees", "--jurisdiction", GJ,
                                  "--kind", "property_claim", "--claim-amount", bad])
        assert code == 2, bad
        assert "--claim-amount" in err, (bad, err)


def test_cli_fees_rejects_second_dot():
    code, out, err = run_cli(["fees", "--jurisdiction", GJ, "--kind", "property_claim",
                              "--claim-amount", "1.2.3"])
    assert code == 2
    assert "--claim-amount" in err


def test_cli_fees_zero_still_goes_through_the_engine():
    """0/отрицательные — как раньше: отказ на уровне движка, не argparse."""
    code, out, err = _fees("0")
    assert code == 1
    assert "positive" in err


# ------------------------------------------------------------------ engine


def test_engine_accepts_decimal_claim_amount():
    res = calculate_fee(claim_amount=Decimal("3697453.16"), jurisdiction=GJ,
                        fee_kind="property_claim", rules=load_rules())
    assert res.status == STATUS_OK
    assert res.amount == 49882
    assert isinstance(res.amount, int), "в JSON-контракт обязан уходить целый рубль"


def test_engine_rounds_only_the_total():
    rules = load_rules()
    with_kopecks = calculate_fee(claim_amount=Decimal("4000071.43"), jurisdiction=GJ,
                                 fee_kind="property_claim", rules=rules)
    whole = calculate_fee(claim_amount=4000071, jurisdiction=GJ,
                          fee_kind="property_claim", rules=rules)
    assert with_kopecks.amount == 52001
    assert whole.amount == 52000


def test_engine_share_path_uses_the_decimal_base():
    """Судебный приказ = 50% пошлины: 52 001 × 0.5 = 26 000.5 → 26 001."""
    res = calculate_fee(claim_amount=Decimal("4000071.43"), jurisdiction=GJ,
                        fee_kind="court_order", rules=load_rules())
    assert res.status == STATUS_OK
    assert res.amount == 26001


def test_engine_still_refuses_bool_and_float():
    """bool и floatgate остаются: float — не денежный тип (ТЗ: bool запрещён)."""
    rules = load_rules()
    assert calculate_fee(claim_amount=True, jurisdiction=GJ,
                         fee_kind="property_claim", rules=rules).status == STATUS_MISSING_PARAM
    assert calculate_fee(claim_amount=1000.5, jurisdiction=GJ,
                         fee_kind="property_claim", rules=rules).status == STATUS_MISSING_PARAM


def test_engine_rejects_non_positive_decimal():
    rules = load_rules()
    for bad in (Decimal("0"), Decimal("-5.50")):
        res = calculate_fee(claim_amount=bad, jurisdiction=GJ,
                            fee_kind="property_claim", rules=rules)
        assert res.status == STATUS_MANUAL_REVIEW, bad
