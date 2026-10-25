"""v2.10.0 W4d (S-41): дата-привязка расчёта госпошлины на VERIFIED историческом тарифе.

Пакет ``historical_court_tariffs`` в ``legal_rules.json`` (VERIFIED colleague-legal,
the compliance file) несёт две редакции
ст. 333.19/333.21 НК РФ:

* ``nk_rf_221_fz_2015``  — 2015-01-01 .. 2024-09-08 (границы включительные);
* ``nk_rf_259_fz_2024``  — 2024-09-09 .. (действующая, сериализована в этом же
  файле в блоке ``jisdictions``).

Ожидания не берутся из цифр акта: каждый кейс пересчитывается независимым
проходом по брекетам, прочитанным из rules-JSON, и сравнивается с движком.
Отдельный тест доказывает, что чтение «base + rate от суммы свыше» и посуммное
чтение «кусков по интервалам» дают одно число — то есть данные однозначны и
подгонки под эталонную сумму нет.

Обратная совместимость тоже факт теста: вызов без ``as_of`` обязан отдавать
байт-в-байт прежний конверт v2.9.
"""
import copy
import inspect
import json
import math
from datetime import date
from decimal import Decimal

import pytest

from sud_cli import legal_engine
from sud_cli.legal_engine import (
    AS_OF_PRE_VERIFIED_WARNING,
    STATUS_MANUAL_REVIEW,
    STATUS_MISSING_PARAM,
    STATUS_OK,
    acting_edition,
    calculate_fee,
    load_rules,
    parse_as_of,
    select_tariff_edition,
    tariff_editions,
)
from tests.conftest import run_cli

ARBITRATION = "arbitration"
GEN_JUR = "general_jurisdiction"
ED_221 = "nk_rf_221_fz_2015"
ED_259 = "nk_rf_259_fz_2024"
#: Published envelope of v2.9 — any new key here is a contract change.
LEGACY_ENVELOPE_KEYS = {"status", "amount_rub", "detail", "formula", "breakdown"}


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _edition_221(rules):
    edition = next(e for e in tariff_editions(rules) if e["edition_id"] == ED_221)
    assert edition.get("jurisdictions"), "pack must ship its own historical tables"
    return edition


def _spec(rules, jurisdiction, fee_kind):
    return _edition_221(rules)["jurisdictions"][jurisdiction]["fee"][fee_kind]


def _round_half_up(value: Decimal) -> int:
    """Независимая реализация п. 6 ст. 52 НК РФ для эталона теста."""
    return math.floor(_dec(value) + Decimal("0.5"))


def _piecewise(amount, brackets) -> Decimal:
    """Эталон «кусками»: каждый интервал платит со своей части цены иска.

    Специально не похож на движок: движок берёт один покрывающий брекет и его
    ``base``; здесь складываются все пройденные интервалы, и ``base`` поздних
    брекетов не участвует вообще (плоский минимум текущей шкалы учитывается как
    значение whole-интервала).
    """
    amount = _dec(amount)
    total = Decimal("0")
    lower = Decimal("0")
    for bracket in brackets:
        rate_on = bracket.get("rate_on") or "over"
        rate = _dec(bracket["rate"])
        up_to = bracket["up_to"]
        upper = _dec(up_to) if up_to is not None else amount
        end = amount if amount < upper else upper
        if rate_on == "threshold":
            total += _dec(bracket["base"])
        elif rate_on == "full":
            total += rate * (end - lower)
        else:  # over / excess
            total += rate * max(end - lower, Decimal("0"))
        if up_to is None or amount <= upper:
            return total
        lower = upper
    raise AssertionError("progressive table has no open-ended bracket")


def _independent_progressive(spec, amount) -> int:
    rubles = _round_half_up(_piecewise(amount, spec["brackets"]))
    if spec.get("floor") is not None:
        rubles = max(rubles, int(spec["floor"]))
    if spec.get("ceiling") is not None:
        rubles = min(rubles, int(spec["ceiling"]))
    return rubles


def _expected_edition_id(rules, as_of: date) -> str:
    """Ожидание по окнам из самого пакета — не вызов прод-функции selection."""
    for edition in tariff_editions(rules):
        start = date.fromisoformat(edition["effective_from"])
        end = date.fromisoformat(edition["effective_to"]) if edition.get("effective_to") else None
        if as_of >= start and (end is None or as_of <= end):
            return edition["edition_id"]
    return acting_edition(rules)["edition_id"]


# --------------------------------------------------------------- исторический расчёт


def test_historical_property_arbitration_matches_independent_table(rules):
    """3 697 453.16 @2020, ст. 333.21 ред. 221-ФЗ: 33 000 + 0,5% от 1 697 453.16."""
    spec = _spec(rules, ARBITRATION, "property_claim")
    amount = Decimal("3697453.16")
    expected = _independent_progressive(spec, amount)
    res = calculate_fee(claim_amount=amount, jurisdiction=ARBITRATION,
                        fee_kind="property_claim", rules=rules, as_of="2020-06-01")
    assert res.status == STATUS_OK
    # ручная арифметика по брекетам из JSON: 33000 + 8487.2658 -> 41487
    assert expected == 41487
    assert res.amount == expected
    single_bracket = _dec(spec["brackets"][-1]["base"]) + _dec(spec["brackets"][-1]["rate"]) * (
        amount - _dec(spec["brackets"][-2]["up_to"]))
    assert _round_half_up(single_bracket) == expected, "движок читает брекеты иначе эталона"
    assert res.meta["edition_id"] == ED_221
    assert "ст. 333.21" in res.formula


def test_historical_property_general_jurisdiction_matches_independent_table(rules):
    """Та же цена в СОЮ @2020 (ст. 333.19 ред. 221-ФЗ): 13 200 + 0,5% от 2 697 453.16."""
    spec = _spec(rules, GEN_JUR, "property_claim")
    amount = Decimal("3697453.16")
    res = calculate_fee(claim_amount=amount, jurisdiction=GEN_JUR,
                        fee_kind="property_claim", rules=rules, as_of=date(2020, 6, 1))
    assert res.amount == _independent_progressive(spec, amount) == 26687
    current = calculate_fee(claim_amount=amount, jurisdiction=GEN_JUR,
                            fee_kind="property_claim", rules=rules)
    assert current.amount == 49882 != res.amount, "исторический расчёт не должен быть «сегодняшним»"


@pytest.mark.parametrize("jurisdiction", [ARBITRATION, GEN_JUR])
def test_historical_bases_are_cumulative_so_both_readings_agree(rules, jurisdiction):
    """``base`` брекета = сумма предыдущих интервалов — двойного смысла в данных нет.

    Если бы это не выполнялось, «base + rate от суммы» и посуммное чтение дали бы
    разные деньги, и у теста не было бы эталона. Проверяется прямо по JSON.
    """
    brackets = _spec(rules, jurisdiction, "property_claim")["brackets"]
    for index in range(1, len(brackets)):
        lower_edge = _dec(brackets[index - 1]["up_to"])
        assert _dec(brackets[index]["base"]) == _piecewise(lower_edge, brackets), (
            f"{jurisdiction} bracket {index}: base != сумма предыдущих интервалов"
        )


def test_historical_fixed_by_applicant_rates(rules):
    """Неимущественный иск СОЮ @2020: 300 / 6000 из редакции, а не 3000 / 20000."""
    spec = _spec(rules, GEN_JUR, "non_property_claim")
    for applicant in ("individual", "entity"):
        res = calculate_fee(jurisdiction=GEN_JUR, fee_kind="non_property_claim",
                            applicant_type=applicant, rules=rules, as_of="2022-03-01")
        assert res.status == STATUS_OK
        assert res.amount == int(spec[applicant])
        assert res.meta["edition_id"] == ED_221
    current = calculate_fee(jurisdiction=GEN_JUR, fee_kind="non_property_claim",
                            applicant_type="individual", rules=rules)
    assert current.amount == 3000


def test_fixed_appeal_2015_differs_from_2024_12(rules):
    """flat ``fixed`` из 221-ФЗ (3000, заявитель не нужен) против шкалы 259-ФЗ."""
    spec = _spec(rules, ARBITRATION, "appeal")
    assert spec["type"] == "fixed"
    historical = calculate_fee(jurisdiction=ARBITRATION, fee_kind="appeal",
                               rules=rules, as_of="2015-06-01")
    assert historical.status == STATUS_OK
    assert historical.amount == int(spec["amount"]) == 3000
    assert historical.meta["edition_id"] == ED_221
    assert historical.meta["basis"].endswith("221-ФЗ")

    current = calculate_fee(jurisdiction=ARBITRATION, fee_kind="appeal",
                            applicant_type="entity", rules=rules, as_of="2024-12-01")
    assert current.status == STATUS_OK and current.amount == 30000
    assert current.amount != historical.amount
    assert current.meta["edition_id"] == ED_259
    # действующей ставке нужен заявитель, историческая flat-ставка его не требует
    missing = calculate_fee(jurisdiction=ARBITRATION, fee_kind="appeal",
                            rules=rules, as_of="2024-12-01")
    assert missing.status == STATUS_MISSING_PARAM
    assert missing.meta["edition_id"] == ED_259


def test_historical_floor_applies(rules):
    """floor редакции — 2000 руб. (ст. 333.21 ред. 221-ФЗ) на малой цене иска."""
    assert int(_spec(rules, ARBITRATION, "property_claim")["floor"]) == 2000
    for amount in (1000, 20000, 40000, 49999):
        res = calculate_fee(claim_amount=amount, jurisdiction=ARBITRATION,
                            fee_kind="property_claim", rules=rules, as_of="2020-01-01")
        assert res.amount == 2000, amount
    exact = calculate_fee(claim_amount=50000, jurisdiction=ARBITRATION,
                          fee_kind="property_claim", rules=rules, as_of="2020-01-01")
    assert exact.amount == 2000  # ровно 4% от 50 000 — минимум не «съедает» шкалу


def test_historical_ceiling_applies(rules):
    """ceiling редакции — 200 000 руб.: выше точки clamp потолок, ниже — без него."""
    spec = _spec(rules, ARBITRATION, "property_claim")
    assert int(spec["ceiling"]) == 200000
    clamped = calculate_fee(claim_amount=50_000_000, jurisdiction=ARBITRATION,
                            fee_kind="property_claim", rules=rules, as_of="2020-01-01")
    assert clamped.amount == 200000
    assert "потолок" in clamped.detail
    under = calculate_fee(claim_amount=35_300_000, jurisdiction=ARBITRATION,
                          fee_kind="property_claim", rules=rules, as_of="2020-01-01")
    assert under.amount == _independent_progressive(spec, 35_300_000) == 199500
    assert "потолок" not in under.detail


def test_historical_share_kind_follows_historical_base(rules):
    """Судебный приказ @2020 = 50% исторической пошлины, floor 0 из той же редакции."""
    property_spec = _spec(rules, ARBITRATION, "property_claim")
    order_spec = _spec(rules, ARBITRATION, "court_order")
    amount = Decimal("3697453.16")
    base_fee = calculate_fee(claim_amount=amount, jurisdiction=ARBITRATION,
                             fee_kind="property_claim", rules=rules, as_of="2020-06-01")
    order = calculate_fee(claim_amount=amount, jurisdiction=ARBITRATION,
                          fee_kind="court_order", rules=rules, as_of="2020-06-01")
    assert base_fee.status == STATUS_OK and order.status == STATUS_OK
    assert base_fee.amount == _independent_progressive(property_spec, amount)
    assert order.amount == _round_half_up(_dec(base_fee.amount) * _dec(order_spec["factor"]))
    assert order.amount == 20744
    assert order.meta["edition_id"] == ED_221
    # малая цена: 50% от floor-пошлины = 1000; исторический floor 0 её не поднимает
    small = calculate_fee(claim_amount=1000, jurisdiction=ARBITRATION,
                          fee_kind="court_order", rules=rules, as_of="2020-06-01")
    assert small.amount == 1000
    current_small = calculate_fee(claim_amount=1000, jurisdiction=ARBITRATION,
                                  fee_kind="court_order", rules=rules)
    assert current_small.amount == 8000  # действующий floor 8000 — другая редакция


def test_kas_not_in_historical_pack_fails_closed(rules):
    """КАС в пакете 221-ФЗ не сериализован — молча подставлять таблицу СОЮ нельзя."""
    res = calculate_fee(jurisdiction="kas", fee_kind="non_property_claim",
                        applicant_type="entity", rules=rules, as_of="2020-01-01")
    assert res.status == STATUS_MANUAL_REVIEW
    assert res.amount is None
    assert ED_221 in res.detail and "kas" in res.detail
    assert "MANUAL_LEGAL_REVIEW_REQUIRED" in res.detail
    assert res.meta["edition_id"] == ED_221


def test_fee_kind_missing_in_historical_edition_fails_closed(rules):
    """bankruptcy_creditor_claim есть в действующей шкале, в таблице 221-ФЗ его нет."""
    res = calculate_fee(jurisdiction=ARBITRATION, fee_kind="bankruptcy_creditor_claim",
                        claim_amount=1_000_000, rules=rules, as_of="2019-05-01")
    assert res.status == STATUS_MANUAL_REVIEW and res.amount is None
    assert res.meta["edition_id"] == ED_221


# ------------------------------------------------------------ границы и выбор редакции


def test_edition_windows_are_inclusive_on_both_sides(rules):
    for iso in ("2015-01-01", "2024-09-08"):
        edition, warnings = select_tariff_edition(rules, date.fromisoformat(iso))
        assert edition["edition_id"] == ED_221, iso
        assert warnings == [], iso
        res = calculate_fee(claim_amount=100_000, jurisdiction=ARBITRATION,
                            fee_kind="property_claim", rules=rules, as_of=iso)
        assert res.amount == 4000, iso          # 4% от суммы, floor не мешает
        assert res.meta["effective_to"] == "2024-09-08"


def test_day_after_historical_window_is_current_without_warning(rules):
    edition, warnings = select_tariff_edition(rules, date(2024, 9, 9))
    assert edition["edition_id"] == ED_259
    assert warnings == []
    res = calculate_fee(claim_amount=100_000, jurisdiction=ARBITRATION,
                        fee_kind="property_claim", rules=rules, as_of="2024-09-09")
    assert res.amount == 10000                   # действующий flat-минимум
    assert res.meta["edition_id"] == ED_259
    assert res.meta["effective_to"] is None
    assert res.meta["tariff_effective_window"] == "2024-09-09..без даты окончания"
    assert "warnings" not in res.meta


def test_as_of_before_first_edition_is_manual_review():
    """Акт legal п.3: дата < 2015-01-01 = manual_review без суммы (не подмена)."""
    from sud_cli.legal_engine import calculate_fee, load_rules
    res = calculate_fee(claim_amount=Decimal("100000"), jurisdiction="arbitration",
                        fee_kind="property_claim", claim_class="improperty",
                        as_of="2010-01-01", rules=load_rules())
    d = res.to_dict()
    assert d["status"] != "ok" and d.get("amount_rub") in (None, "")
    assert "2015-01-01" in str(d)



def test_acting_edition_is_the_open_ended_one(rules):
    acting = acting_edition(rules)
    assert acting["edition_id"] == ED_259
    assert acting["effective_to"] is None
    assert "259-ФЗ" in acting["basis"]


# ------------------------------------------------------------------------ provenance


@pytest.mark.parametrize("iso", ["2015-01-01", "2019-11-05", "2024-09-08",
                                 "2024-09-09", "2026-10-24", "2020-06-01"])
@pytest.mark.parametrize("kwargs", [
    {"jurisdiction": ARBITRATION, "fee_kind": "property_claim", "claim_amount": 250000},
    {"jurisdiction": GEN_JUR, "fee_kind": "appeal", "applicant_type": "individual"},
    {"jurisdiction": ARBITRATION, "fee_kind": "appeal"},
    {"jurisdiction": GEN_JUR, "fee_kind": "property_claim"},  # MISSING claim_amount
])
def test_provenance_present_in_every_as_of_output(rules, iso, kwargs):
    res = calculate_fee(rules=rules, as_of=iso, **kwargs)
    assert res.meta, f"no provenance for {iso} {kwargs}"
    for key in ("edition_id", "effective_from", "effective_to", "basis",
                "tariff_effective_window", "as_of"):
        assert key in res.meta, key
    assert res.meta["as_of"] == iso
    assert res.meta["tariff_effective_window"].startswith(res.meta["effective_from"])
    assert res.meta["basis"].startswith("Федеральный закон от")
    assert res.meta["edition_id"] == _expected_edition_id(rules, date.fromisoformat(iso))
    assert "meta" in res.to_dict()


def test_as_of_none_is_byte_identical_to_omitted(rules):
    """регресс-пин: явный ``as_of=None`` и вызов без него — один и тот же конверт."""
    matrix = [
        {"claim_amount": Decimal("3697453.16"), "jurisdiction": GEN_JUR,
         "fee_kind": "property_claim"},
        {"claim_amount": 2_000_000_000, "jurisdiction": ARBITRATION,
         "fee_kind": "property_claim"},
        {"claim_amount": 1000, "jurisdiction": ARBITRATION, "fee_kind": "court_order"},
        {"jurisdiction": ARBITRATION, "fee_kind": "appeal", "applicant_type": "entity"},
        {"jurisdiction": "kas", "fee_kind": "non_property_claim", "applicant_type": "entity"},
        {"jurisdiction": "bogus", "fee_kind": "appeal"},
        {"claim_amount": 0, "jurisdiction": ARBITRATION, "fee_kind": "property_claim"},
        {"jurisdiction": ARBITRATION, "fee_kind": "nonsense"},
    ]
    for kwargs in matrix:
        omitted = calculate_fee(rules=rules, **kwargs)
        explicit = calculate_fee(rules=rules, as_of=None, **kwargs)
        assert repr(omitted) == repr(explicit), kwargs
        assert omitted.to_dict() == explicit.to_dict(), kwargs
        assert set(omitted.to_dict()) == LEGACY_ENVELOPE_KEYS, kwargs
        assert omitted.meta == {}


def test_legacy_envelope_values_are_unchanged(rules):
    """Числа и формулы v2.9 на контрольных точках не сдвинулись."""
    pins = [
        ({"claim_amount": Decimal("3697453.16"), "jurisdiction": GEN_JUR,
          "fee_kind": "property_claim"}, 49882, "333.19 НК РФ: 45,000 + 0.7%"),
        ({"claim_amount": 1_000_000, "jurisdiction": ARBITRATION,
          "fee_kind": "property_claim"}, 55000, "333.21 НК РФ: 10,000 + 5%"),
        ({"claim_amount": 1000, "jurisdiction": ARBITRATION,
          "fee_kind": "court_order"}, 8000, "10,000 × 0.5 = 5,000"),
        ({"jurisdiction": ARBITRATION, "fee_kind": "appeal",
          "applicant_type": "entity"}, 30000, "333.21 НК РФ: 30,000 руб."),
    ]
    for kwargs, amount, formula_part in pins:
        res = calculate_fee(rules=rules, **kwargs)
        assert res.amount == amount, kwargs
        assert formula_part in res.formula, (kwargs, res.formula)
        assert "meta" not in res.to_dict()


def test_engine_accepts_date_and_iso_string_identically(rules):
    via_date = calculate_fee(claim_amount=Decimal("3697453.16"), jurisdiction=ARBITRATION,
                             fee_kind="property_claim", rules=rules, as_of=date(2020, 6, 1))
    via_text = calculate_fee(claim_amount=Decimal("3697453.16"), jurisdiction=ARBITRATION,
                             fee_kind="property_claim", rules=rules, as_of="2020-06-01")
    assert via_date.to_dict() == via_text.to_dict()
    assert parse_as_of("2020-06-01") == date(2020, 6, 1)


@pytest.mark.parametrize("bad", ["31.12.2020", "2020-13-01", "2020-02-30", "2020-6-1",
                                 "20200601", "", "2020/06/01", 2020, True, ["2020-06-01"]])
def test_invalid_as_of_is_a_loud_valueerror(bad):
    with pytest.raises(ValueError):
        parse_as_of(bad)
    with pytest.raises(ValueError):
        calculate_fee(jurisdiction=ARBITRATION, fee_kind="appeal",
                      applicant_type="entity", as_of=bad)


def test_legacy_signature_is_compatible():
    """regress: все прежние параметры keyword-only и на месте; as_of — опциональный."""
    params = inspect.signature(calculate_fee).parameters
    assert params["as_of"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["as_of"].default is None
    for name in ("claim_amount", "jurisdiction", "fee_kind", "applicant_type",
                 "claim_class", "rules"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    with pytest.raises(TypeError):
        calculate_fee(1000, ARBITRATION, "property_claim")
    res = calculate_fee(claim_amount=1000, jurisdiction=ARBITRATION,
                        fee_kind="property_claim", applicant_type=None,
                        claim_class=None, rules=load_rules())
    assert res.status == STATUS_OK and res.amount == 10000


def test_rates_are_read_from_rules_not_hardcoded(rules):
    """Мутация floor в копии правил меняет ответ — цифра не «вшита» в код."""
    mutated = copy.deepcopy(rules)
    spec = mutated["historical_court_tariffs"]["editions"][0]["jurisdictions"][GEN_JUR]["fee"]
    assert spec["property_claim"]["floor"] == 400
    spec["property_claim"]["floor"] = 5000
    res = calculate_fee(claim_amount=1000, jurisdiction=GEN_JUR,
                        fee_kind="property_claim", rules=mutated, as_of="2016-01-01")
    assert res.amount == 5000
    untouched = calculate_fee(claim_amount=1000, jurisdiction=GEN_JUR,
                              fee_kind="property_claim", rules=rules, as_of="2016-01-01")
    assert untouched.amount == 400, "оригинальные правила не изменены мутацией"


def test_pack_without_editions_warns_instead_of_inventing(rules):
    """Правила без исторического пакета: ответ по действующей базе + видимый warning."""
    stripped = copy.deepcopy(rules)
    stripped.pop("historical_court_tariffs")
    res = calculate_fee(claim_amount=1_000_000, jurisdiction=ARBITRATION,
                        fee_kind="property_claim", rules=stripped, as_of="2019-01-01")
    assert res.status == STATUS_OK and res.amount == 55000
    assert res.meta["warnings"] == [legal_engine.AS_OF_OUTSIDE_WINDOWS_WARNING]
    assert res.meta["edition_id"] == stripped["tariff_version"]


def test_edition_order_in_the_pack_does_not_change_the_answer(rules):
    """Окна пересчитываются по датам, а не по порядку списка в JSON."""
    shuffled = copy.deepcopy(rules)
    shuffled["historical_court_tariffs"]["editions"] = list(
        reversed(shuffled["historical_court_tariffs"]["editions"])
    )
    for iso in ("2015-01-01", "2020-06-01", "2024-09-09"):
        ordered = calculate_fee(claim_amount=300_000, jurisdiction=ARBITRATION,
                                fee_kind="property_claim", rules=rules, as_of=iso)
        reversed_pack = calculate_fee(claim_amount=300_000, jurisdiction=ARBITRATION,
                                      fee_kind="property_claim", rules=shuffled, as_of=iso)
        assert ordered.to_dict() == reversed_pack.to_dict(), iso


@pytest.mark.parametrize("jurisdiction", [ARBITRATION, GEN_JUR])
def test_bracket_min_max_agree_with_edition_floor_ceiling(rules, jurisdiction):
    """``min_fee``/``max_fee`` внутри брекетов равны ``floor``/``ceiling`` вида пошлины.

    Применяется только парная проверка уровня вида (один clamp после оценки
    шкалы). Если будущий пакет разведёт эти значения, тест красный: молча
    расходящиеся минимумы в юридическом калькуляторе недопустимы.
    """
    spec = _spec(rules, jurisdiction, "property_claim")
    floors = [b["min_fee"] for b in spec["brackets"] if "min_fee" in b]
    ceilings = [b["max_fee"] for b in spec["brackets"] if "max_fee" in b]
    assert floors == [spec["floor"]], (jurisdiction, floors, spec["floor"])
    assert ceilings == [spec["ceiling"]], (jurisdiction, ceilings, spec["ceiling"])


def test_single_shared_progressive_helper_serves_both_scales(monkeypatch, rules):
    """Один eval-хелпер (task-briefd §1): обе шкалы идут через ``_eval_progressive``."""
    seen = []
    real = legal_engine._eval_progressive

    def spy(raw, brackets):
        seen.append(len(brackets))
        return real(raw, brackets)

    monkeypatch.setattr(legal_engine, "_eval_progressive", spy)
    calculate_fee(claim_amount=500_000, jurisdiction=ARBITRATION,
                  fee_kind="property_claim", rules=rules)
    calculate_fee(claim_amount=500_000, jurisdiction=ARBITRATION,
                  fee_kind="property_claim", rules=rules, as_of="2020-01-01")
    assert len(seen) == 2, "у шкал должен быть один общий eval-хелпер"
    assert not hasattr(legal_engine, "_progressive"), "старый отдельный путь должен исчезнуть"


# -------------------------------------------------------------------------- CLI


def test_cli_as_of_emits_provenance_in_meta_and_data(tmp_path):
    code, out, err = run_cli(["--json", "fees", "--jurisdiction", GEN_JUR,
                              "--kind", "property_claim", "--claim-amount", "3697453.16",
                              "--as-of", "2020-06-01"],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code == 0, err
    doc = json.loads(out)
    assert doc["data"]["amount_rub"] == 26687
    assert doc["data"]["meta"]["edition_id"] == ED_221
    assert doc["meta"]["tariff_edition_id"] == ED_221
    assert doc["meta"]["as_of"] == "2020-06-01"
    assert doc["meta"]["tariff_effective_window"] == "2015-01-01..2024-09-08"
    assert "221-ФЗ" in doc["meta"]["tariff_basis"]


def test_cli_without_as_of_keeps_legacy_envelope(tmp_path):
    code, out, err = run_cli(["--json", "fees", "--jurisdiction", GEN_JUR,
                              "--kind", "property_claim", "--claim-amount", "3697453.16"],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code == 0, err
    doc = json.loads(out)
    assert doc["data"]["amount_rub"] == 49882
    assert set(doc["data"]) == LEGACY_ENVELOPE_KEYS | {"disclaimer", "exemptions_note",
                                                       "clean_room"}
    assert "as_of" not in doc["meta"]
    assert "tariff_edition_id" not in doc["meta"]


def test_cli_invalid_as_of_refuses_with_invalid_status(tmp_path):
    code, out, err = run_cli(["--json", "fees", "--jurisdiction", ARBITRATION,
                              "--kind", "property_claim", "--claim-amount", "1000",
                              "--as-of", "31.12.2020"],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code != 0
    assert out == ""
    doc = json.loads(err)
    assert "INVALID" in doc["data"]["status"]
    assert "amount_rub" not in json.dumps(doc, ensure_ascii=False)


def test_cli_as_of_before_pack_prints_warning(tmp_path):
    """Акт legal п.3: CLI для даты < 2015 = manual_review без суммы, exit != 0."""
    code, out, err = run_cli(["--json", "fees", "--jurisdiction", ARBITRATION,
                              "--kind", "property_claim", "--claim-amount", "1000",
                              "--as-of", "2010-05-09"],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code != 0
    doc = json.loads(out or err)
    assert doc["data"]["status"] == "MANUAL_LEGAL_REVIEW_REQUIRED"
    assert doc["data"].get("amount_rub") in (None, "", 0)
    assert "2015-01-01" in str(doc)



def test_cli_as_of_rejects_unknown_jurisdiction_history(tmp_path):
    """КАС @2020 через CLI: отказ без числа, а не «текущий тариф под исторической датой»."""
    code, out, err = run_cli(["--json", "fees", "--jurisdiction", "kas",
                              "--kind", "non_property_claim", "--applicant", "entity",
                              "--as-of", "2020-01-01"],
                             env={"SUD_CLI_STATE_DIR": str(tmp_path)})
    assert code != 0
    doc = json.loads(err)
    assert doc["data"]["status"] == STATUS_MANUAL_REVIEW
    assert doc["data"]["amount_rub"] is None, "отказ не имеет права выдавать сумму"
    assert doc["meta"]["tariff_edition_id"] == ED_221


# --------------------------------------------------------------------------- MCP


def _mcp_call(arguments):
    from sud_cli import mcp_server

    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                                      "params": {"name": "sud_cli_fees",
                                                 "arguments": arguments}})
    result = resp["result"]
    text = result["content"][0]["text"]
    try:
        return result.get("isError", False), json.loads(text)
    except json.JSONDecodeError:
        return result.get("isError", False), text


def _mcp_fees_schema():
    from sud_cli import mcp_server

    return next(t for t in mcp_server.TOOLS if t["name"] == "sud_cli_fees")["schema"]


def test_mcp_schema_declares_as_of_as_date_string():
    props = _mcp_fees_schema()["properties"]
    assert "as_of" in props
    assert props["as_of"]["type"] == "string"
    assert props["as_of"]["format"] == "date"
    assert props["as_of"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert _mcp_fees_schema()["additionalProperties"] is False


def test_mcp_tool_count_did_not_grow():
    from sud_cli import mcp_server

    # v2.11.0 W6a §1: счётчик вырос 35 -> 45 — рост плановый (10 офлайн-тулов
    # паритета), а НЕ от as_of: as_of остаётся свойством схемы fees без нового
    # тула, как и задумывалось в v2.10.0.
    # v2.12.0 the wave §4: 45 -> 46 — ровно +1 read-тул sud_cli_form_new (формы).
    # v2.13.0 история §5: 46 -> 58 — +12 тулов волны «портфель и данные»
    # (chain/rep/conflicts/reports/metrics/backup/export/purge); as_of по-прежнему
    # свойство схемы fees, а не тул.
    assert len(mcp_server.TOOLS) == 65, ("as_of — свойство схемы, а не новый тул; "
                                         "+10 тулов — план W6a §1; +1 — the wave §4; "
                                         "+12 — история §5; +1 — §13-интерфейс license")


def test_mcp_dispatch_passes_as_of_through_alias_names():
    is_error, body = _mcp_call({"jurisdiction": "arbitral", "kind": "property_claim",
                                "claim_amount": 3697453.16, "as_of": "2020-06-01"})
    assert is_error is False, body
    assert body["amount_rub"] == 41487
    assert body["meta"]["edition_id"] == ED_221
    assert body["meta"]["basis"].endswith("221-ФЗ")


def test_mcp_without_as_of_is_unchanged():
    is_error, body = _mcp_call({"jurisdiction": "general_jurisdiction",
                                "fee_kind": "property_claim", "claim_amount": 3697453.16})
    assert is_error is False, body
    assert body["amount_rub"] == 49882
    assert "meta" not in body


def test_mcp_invalid_as_of_is_typed_error_not_a_number():
    for bad in ("31.12.2020", 2020, True, "2020-6-1"):
        is_error, body = _mcp_call({"jurisdiction": ARBITRATION, "fee_kind": "appeal",
                                    "applicant_type": "entity", "as_of": bad})
        assert is_error is True, (bad, body)
        assert "as_of" in str(body)
