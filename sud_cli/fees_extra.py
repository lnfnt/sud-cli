"""W4c: льготы 333.35-37, возврат 333.40, рамка индексации 183/208/189.1.

Основания — ТОЛЬКО fee_exemptions_and_refunds из legal_rules.json
(VERIFIED pack colleague-legal, акт 2026.10.24). Список категорий в акте —
выборка, а не полный перечень статьи: совпадения нет => manual_review с
fetch_hint, права додумывать категории движок не имеет.
Индексация считается ТОЛЬКО по ИПЦ, явно переданному оператором (данных
Росстата в репозитории нет; выдумывать коэффициент запрещено).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

DISCLAIMER = ("Расчёт справочный: льготы применяются судом по представленным "
              "документам; категории — VERIFIED-выборка legal, не полный перечень статьи.")

_JURIS_TO_EXEMPT_KEY = {
    "apk": "exemptions_333_37",
    "gpk": "exemptions_333_36",
    "sayu": "exemptions_333_36",
    "kas": "exemptions_333_36",
}


def _pack(rules: dict) -> dict:
    return rules.get("fee_exemptions_and_refunds", {})


def fee_exemption_check(rules: dict, *, jurisdiction: str, category_code: str) -> dict:
    pack = _pack(rules)
    keys = [_JURIS_TO_EXEMPT_KEY.get(str(jurisdiction).lower()), "exemptions_333_35"]
    hits = []
    for k in keys:
        if not k:
            continue
        for cat in pack.get(k, {}).get("categories", []):
            if cat.get("code") == category_code:
                hits.append({**cat, "statute": pack[k]["statute"]})
    if hits:
        full = [h for h in hits if h.get("exempt_level") == "full"] or hits
        h = full[0]
        return {"status": "exempt", "exempt_level": h.get("exempt_level"),
                "basis": h.get("basis"), "description": h.get("description"),
                "statute": h.get("statute"), "warnings": [DISCLAIMER]}
    return {"status": "manual_review",
            "detail": f"категория '{category_code}' не в VERIFIED-выборке; полный текст "
                      f"ст.333.35-37 — npa_as_of, не догадка движка",
            "fetch_hint": "npa_as_of", "warnings": [DISCLAIMER]}


def fee_refund(rules: dict, *, paid: Decimal, ground: str,
               claim_admitted: bool = False, plaintiff_default: bool = False,
               transferred_to_arbitration: bool = False) -> dict:
    pack = _pack(rules)
    grounds = {g["code"]: g for g in pack.get("refund_333_40", {}).get("grounds", [])}
    if ground not in grounds:
        return {"status": "manual_review", "detail": f"основание '{ground}' не из VERIFIED списка",
                "known": sorted(grounds), "warnings": [DISCLAIMER]}
    g = grounds[ground]
    share = Decimal(str(g.get("refund_share", 1)))
    notes = [g.get("condition", "")]
    if ground == "dismissed_no_consideration" and (plaintiff_default or transferred_to_arbitration):
        share = Decimal("0")
        notes.append("исключение VERIFIED-условия: неявка истца / передача в третейский суд — возврат не положен")
    if ground.startswith("settlement") and claim_admitted:
        share = Decimal("0")
        notes.append("исключение VERIFIED-условия: признание иска ответчиком — 70/50/30% не применяются")
    amount = (paid * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"status": "ok", "ground": ground, "basis": g["basis"], "share": str(share),
            "paid": str(paid), "refund_amount": str(amount), "conditions": [n for n in notes if n],
            "statute": pack.get("refund_333_40", {}).get("statute", "ст. 333.40 НК РФ"),
            "warnings": [DISCLAIMER]}


def indexation_frame(rules: dict, *, code: str) -> dict:
    idx = _pack(rules).get("interest_and_indexation", {}).get("indexation_procedural", {})
    frame = idx.get(str(code).lower())
    if not frame:
        return {"status": "manual_review", "detail": f"нет VERIFIED рамки для '{code}'",
                "known": sorted(idx)}
    return {"status": "frame_only", "index": frame.get("index"),
            "period_start": frame.get("period_start"), "period_end": frame.get("period_end"),
            "statute": frame.get("statute"),
            "calc_hint": "sud-cli fees indexation --code X --amount S --cpi-start A --cpi-end B "
                         "(ИПЦ явно оператором; данных Росстата в пакете нет)",
            "warnings": ["ИПЦ Росстата в репозитории нет: расчёт только по явно переданному "
                         "официальному значению; выдуманный коэффициент = фальсификация (правило «тексты только из реестра»)."]}


def indexation_compute(rules: dict, *, code: str, amount: Decimal,
                       cpi_start: Optional[Decimal], cpi_end: Optional[Decimal]) -> dict:
    f = indexation_frame(rules, code=code)
    if f["status"] != "frame_only":
        return f
    if cpi_start is None or cpi_end is None or cpi_start <= 0:
        return {**f, "status": "data_unavailable",
                "detail": "нужны --cpi-start/--cpi-end (официальные значения ИПЦ Росстата)"}
    indexed = (amount * (cpi_end / cpi_start)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"status": "ok", "basis": f["statute"], "amount": str(amount),
            "cpi_start": str(cpi_start), "cpi_end": str(cpi_end),
            "indexed_amount": str(indexed), "provenance": "user-supplied CPI, unverified against Росстат",
            "period": {"start": f["period_start"], "end": f["period_end"]},
            "warnings": [DISCLAIMER]}
