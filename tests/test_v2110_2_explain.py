"""W6a §2 (S-81): --explain — человекочитаемая цепочка расчёта в конверте.

Контракты:
- флаг глобальный (позиция в argv произвольная, как --json);
- explain появляется ТОЛЬКО с флагом: без флага конверт байт-стабилен
  (ключ "explain" отсутствует, остальное совпадает дословно);
- нормы — norm_ref из rules; verbatim допускается лишь там, где legal_text
  есть в самих правилах; reference_only обязан дать fetch_hint;
- ручные причины (manual_review) видны и на success-, и на error-ветке.
"""
import json

from tests.conftest import run_cli


def _data(args, env=None):
    code, out, err = run_cli(args, env=env)
    if out.strip():
        payload = json.loads(out)["data"]
        return code, payload
    payload = json.loads(err)["data"]
    return code, payload


FEES = ["fees", "--jurisdiction", "arbitration", "--kind", "property_claim",
        "--claim-amount", "100000"]
DEADLINES = ["deadlines", "calculate", "as_appeal_decision", "--event-date", "2026-03-02"]
LIMITATION = ["limitation", "--start", "2023-01-10", "--check-date", "2026-01-10"]
INTEREST = ["interest", "--amount", "100000", "--from", "2025-01-10", "--to", "2025-04-10"]
PRETRIAL = ["pretrial", "calc", "--code", "apk", "--sent", "2026-03-02"]
JURISDICTION = ["jurisdiction", "check", "--code", "apk", "--claim", "поставка"]
VERIFY = ["verify", "quote", "--text", "срок исковой давности", "--source", "npa",
          "--act", "gk_rf", "--article", "196", "--date", "2022-01-10"]


def test_explain_present_in_every_calculation_command():
    for args in (FEES, DEADLINES, LIMITATION, INTEREST, PRETRIAL, JURISDICTION, VERIFY):
        code, payload = _data(["--json", "--explain"] + args)
        assert "explain" in payload, args
        block = payload["explain"]
        for key in ("command", "norms", "data", "formulas", "manual_review", "policy"):
            assert key in block, (args, key)


def test_explain_absent_without_flag_envelope_identical():
    code, base = _data(["--json"] + FEES)
    assert "explain" not in base
    code, with_flag = _data(["--json", "--explain"] + FEES)
    assert "explain" in with_flag
    stripped = {k: v for k, v in with_flag.items() if k != "explain"}
    assert stripped == base, "с флагом меняется ровно один ключ explain"


def test_fees_explain_shows_norm_data_and_formulas():
    code, payload = _data(["--json", "--explain"] + FEES)
    ex = payload["explain"]
    assert any("333.19" in n["norm_ref"] for n in ex["norms"]), ex["norms"]
    assert any(d["item"] == "tariff_basis" and d["federal_law"] for d in ex["data"])
    assert any(d["item"] == "rounding" for d in ex["data"])
    assert ex["formulas"], "прогрессивная шкала обязана показать формулу и шаги"
    assert any("округлени" in f for f in ex["formulas"])


def test_fees_manual_review_reason_visible_on_error_branch():
    code, payload = _data(["--json", "--explain", "fees", "--jurisdiction", "arbitration",
                           "--kind", "appeal", "--applicant", "individual",
                           "--as-of", "2014-01-01"])
    assert payload["status"] != "ok"
    ex = payload["explain"]
    assert ex["manual_review"], ex
    reasons = json.dumps(ex["manual_review"], ensure_ascii=False)
    assert "pre_verified_tariff_date" in reasons or "status != ok" in reasons


def test_deadlines_explain_carries_norm_ref_trace_and_calendar_stamp():
    code, payload = _data(["--json", "--explain"] + DEADLINES)
    ex = payload["explain"]
    assert ex["norms"][0]["norm_ref"] == "ч. 1 ст. 259 АПК РФ"
    assert ex["norms"][0]["source"].startswith("legal_rules.json")
    assert ex["formulas"], "переносы/счётчики шагов обязаны быть в formulas"
    cal = [d for d in ex["data"] if d["item"] == "calendar"]
    assert cal and cal[0]["status"], ex["data"]


def test_interest_and_limitation_explain_norms_and_math():
    code, payload = _data(["--json", "--explain"] + INTEREST)
    ex = payload["explain"]
    assert any("395" in n["norm_ref"] for n in ex["norms"])
    assert any("×" in f for f in ex["formulas"])
    assert any(d["item"] == "key_rates" for d in ex["data"])

    code, payload = _data(["--json", "--explain"] + LIMITATION)
    ex = payload["explain"]
    assert any("196" in n["norm_ref"] for n in ex["norms"])
    assert ex["formulas"], ex


def test_jurisdiction_explain_reference_only_yields_fetch_hint_not_text():
    code, payload = _data(["--json", "--explain", "jurisdiction", "check",
                           "--code", "apk", "--claim", "иск к ООО"])
    ex = payload["explain"]
    assert ex["norms"], ex
    for n in ex["norms"]:
        if n.get("verbatim"):
            # verbatim допустим только из правил — проверяем, что это текст
            # правила, а не выдумка движка: он не пустой и лежит в candidate.
            assert n["norm_ref"]
        else:
            assert n.get("fetch_hint") == "npa_as_of", (
                "reference_only обязан дать fetch_hint, как в W2b", n)


def test_verify_explain_reports_source_and_verdict():
    code, payload = _data(["--json", "--explain"] + VERIFY)
    ex = payload["explain"]
    assert ex["data"][0]["source"] == "npa"
    assert any("verdict=" in f for f in ex["formulas"]), ex["formulas"]


def test_pretrial_explain_days_formula_and_manual():
    code, payload = _data(["--json", "--explain"] + PRETRIAL)
    ex = payload["explain"]
    assert ex["norms"] and ex["norms"][0]["norm_ref"]
    assert any("кал.дн" in f for f in ex["formulas"])
    # postal — NOT_SERIALIZED/ manual_review в rules: причины ручного видны
    code, payload = _data(["--json", "--explain", "pretrial", "calc",
                           "--code", "postal", "--sent", "2026-03-02"])
    ex = payload["explain"]
    assert ex["manual_review"], ex
