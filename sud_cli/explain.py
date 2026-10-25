"""W6a §2 (S-81): --explain — человекочитаемая цепочка расчёта.

Конверт команды с флагом получает блок ``explain`` из четырёх секций:
какие НОРМЫ применялись (norm_ref из rules; verbatim legal_text там, где он
есть в правилах), какие ДАННЫЕ (тарифная редакция, ставки, штампы
производственного календаря), какие ФОРМУЛЫ (шаги breakdown/trace движка) и
что осталось РУЧНЫМ (manual_review причины).

Zero-граница: explain не печатает тексты норм вне правил — для
reference_only-записей отдаётся fetch_hint="npa_as_of" (тот же контракт, что
W2b в jurisdiction/fees_extra). Байт-стабильность старых конвертов: без
флага --explain этот модуль к конверту не прикасается.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

EXPLAIN_POLICY = ("explain собран только из rules-данных движка; тексты норм вне правил "
                  "не выводятся — reference_only даёт fetch_hint=npa_as_of")


def envelope(command: str, norms: Optional[List[dict]] = None,
             data: Optional[List[dict]] = None, formulas: Optional[List[str]] = None,
             manual_review: Optional[List[Any]] = None) -> dict:
    return {
        "command": command,
        "norms": norms or [],
        "data": data or [],
        "formulas": formulas or [],
        "manual_review": manual_review or [],
        "policy": EXPLAIN_POLICY,
    }


def _norm_from_formula(formula: str) -> Optional[str]:
    if not formula:
        return None
    head = formula.split(":", 1)[0].strip()
    return head or None


def fees_explain(*, fee_doc: dict, rules: dict, as_of_iso: Optional[str]) -> dict:
    basis = rules.get("tariff_basis") or {}
    norms = []
    art = _norm_from_formula(fee_doc.get("formula") or "")
    if art:
        norms.append({"norm_ref": art, "source": "legal_rules.json"})
    norms.append({"norm_ref": "ст. 333.19, 333.21 НК РФ",
                  "source": "legal_rules.json (tariff_basis)"})
    data = [
        {"item": "rules", "version": rules.get("version"),
         "tariff_version": rules.get("tariff_version"),
         "legal_status": rules.get("legal_status")},
        {"item": "tariff_basis", "federal_law": basis.get("federal_law"),
         "effective_from": basis.get("effective_from")},
        {"item": "rounding", **{k: (rules.get("rounding") or {}).get(k)
                                for k in ("article", "mode")}},
    ]
    meta = fee_doc.get("meta") or {}
    if as_of_iso:
        data.append({"item": "tariff_edition", "as_of": as_of_iso,
                     "edition_id": meta.get("edition_id"),
                     "window": meta.get("tariff_effective_window") or meta.get("window"),
                     "warnings": meta.get("warnings") or []})
    formulas = []
    if fee_doc.get("formula"):
        formulas.append(fee_doc["formula"])
    for step in fee_doc.get("breakdown") or []:
        if isinstance(step, dict) and step.get("step"):
            formulas.append(step["step"])
    manual = []
    if fee_doc.get("status") != "ok":
        manual.append({"reason": "status != ok", "detail": fee_doc.get("detail")})
    if meta.get("manual_reason"):
        manual.append({"reason": meta["manual_reason"]})
    return envelope("fees", norms=norms, data=data, formulas=formulas, manual_review=manual)


def deadlines_explain(*, payload: dict, rules: dict, event_date: Optional[str],
                      region: Optional[str], tz: Optional[str]) -> dict:
    cal = rules.get("production_calendar") or {}
    entry = (rules.get("deadline_matrix") or {})
    norms = []
    if payload.get("legal_basis"):
        norms.append({"norm_ref": payload["legal_basis"], "source": "legal_rules.json (deadline_matrix)"})
    data = [
        {"item": "spec", "spec_version": rules.get("spec_version"),
         "legal_status": rules.get("legal_status")},
        {"item": "calendar", "status": cal.get("status"), "as_of": cal.get("as_of"),
         "shift_enabled": cal.get("shift_enabled"),
         "transfers_encoded_through": cal.get("government_transfers_encoded_through")},
        {"item": "inputs", "event_date": event_date, "region": region, "tz": tz},
    ]
    formulas = [t.get("step") for t in payload.get("trace") or [] if isinstance(t, dict) and t.get("step")]
    manual = []
    if payload.get("status") != "ok":
        manual.append({"reason": "status != ok", "detail": payload.get("detail")})
    for w in payload.get("warnings") or []:
        manual.append({"reason": "warning", "detail": w})
    return envelope("deadlines", norms=norms, data=data, formulas=[str(f) for f in formulas], manual_review=manual)


def limitation_explain(*, payload: dict, period_type: str, start: str,
                       check_date: Optional[str]) -> dict:
    norms = [{"norm_ref": n, "source": "limitation.PERIODS (ст.-ссылки движка)"}
             for n in payload.get("legal_basis") or []]
    data = [{"item": "inputs", "period_type": period_type, "start": start,
             "check_date": check_date}]
    formulas = [t.get("step") for t in payload.get("trace") or [] if isinstance(t, dict) and t.get("step")]
    manual = []
    if payload.get("status") != "ok":
        manual.append({"reason": "status != ok", "detail": payload.get("detail")})
    return envelope("limitation", norms=norms, data=data, formulas=[str(f) for f in formulas], manual_review=manual)


def interest_explain(*, payload: dict, article: str, amount, start: str, end: str) -> dict:
    norms = [{"norm_ref": f"ст. {article} ГК РФ", "source": "движок interest.py (ст.-ссылка)"}]
    data = [
        {"item": "inputs", "amount": amount, "start": start, "end": end},
        {"item": "key_rates", **(payload.get("rates_meta") or {})},
    ]
    formulas = []
    for p in payload.get("periods") or []:
        if isinstance(p, dict):
            formulas.append(
                f"{p.get('from')}..{p.get('to')}: {p.get('principal_rub')} × {p.get('days')}/{p.get('year_days', '—')} × {p.get('rate')}% = {p.get('amount_rub')}")
    manual = [{"reason": "rates warning", "detail": w} for w in payload.get("rates_warnings") or []]
    return envelope("interest", norms=norms, data=data, formulas=formulas, manual_review=manual)


def pretrial_explain(*, payload: dict, code: str, sent: Optional[str]) -> dict:
    norms = []
    if payload.get("norm"):
        norms.append({"norm_ref": payload["norm"], "source": "jurisdiction_rules.json (pretrial)"})
    data = [{"item": "rule", "code": code, "rule": payload.get("rule"),
             "days": payload.get("days"), "calendar": payload.get("calendar"),
             "exclude_nonworking_holidays": payload.get("exclude_nonworking_holidays")}]
    formulas = []
    if payload.get("sent") and payload.get("deadline"):
        formulas.append(f"{payload['sent']} + {payload.get('days')} кал.дн -> {payload['deadline']}"
                        + (" (с переносом на рабочий день)" if payload.get("exclude_nonworking_holidays") else ""))
    manual = [{"reason": m.get("reason"), "norm": m.get("norm"), "scope": m.get("scope")}
              for m in payload.get("manual_review") or [] if isinstance(m, dict)]
    if payload.get("error"):
        manual.append({"reason": payload["error"], "detail": str(payload.get("warnings") or "")})
    return envelope("pretrial", norms=norms, data=data, formulas=formulas, manual_review=manual)


def pretrial_consequences_explain(*, payload: dict, code: str, stage: str) -> dict:
    norms = []
    if payload.get("norm"):
        norms.append({"norm_ref": payload["norm"], "source": "jurisdiction_rules.json (consequences)"})
    data = [{"item": "selection", "code": code, "stage": stage,
             "consequence": payload.get("consequence"), "confidence": payload.get("confidence")}]
    manual = []
    if payload.get("confidence") not in (None, "hard"):
        manual.append({"reason": "confidence != hard", "detail": payload.get("confidence")})
    return envelope("pretrial consequences", norms=norms, data=data, formulas=[], manual_review=manual)


def jurisdiction_explain(*, payload: dict, code: str, claim: Optional[str]) -> dict:
    norms = []
    for cand in payload.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        entry = {"norm_ref": cand.get("venue_basis"), "confidence": cand.get("confidence")}
        if cand.get("legal_text"):
            entry["verbatim"] = cand["legal_text"]  # verbatim берётся из правил
        if cand.get("fetch_hint"):
            entry["fetch_hint"] = cand["fetch_hint"]
        norms.append(entry)
    data = [{"item": "inputs", "code": code, "claim": claim,
             "rules_version_key": "jurisdiction_rules.json"}]
    formulas = [f"кандидат #{i + 1}: {c.get('venue_basis')} ({c.get('confidence')})"
                for i, c in enumerate(payload.get("candidates") or []) if isinstance(c, dict)]
    manual = [{"reason": m.get("reason"), "norm": m.get("norm")}
              for m in payload.get("manual_review") or [] if isinstance(m, dict)]
    return envelope("jurisdiction check", norms=norms, data=data, formulas=formulas, manual_review=manual)


def verify_explain(*, payload: dict, text_len: int) -> dict:
    data = [{"item": "source", "source": payload.get("source"),
             "location": payload.get("location"),
             "path": payload.get("path"), "case_id": payload.get("case_id"),
             "max_fuzzy": payload.get("max_fuzzy"), "requested_len": text_len}]
    formulas = [f"verdict={payload.get('status')} offset={payload.get('offset')} ratio={payload.get('ratio')}"]
    manual = []
    if payload.get("status") in ("not_found", "edition_unavailable"):
        manual.append({"reason": payload.get("status"), "detail": payload.get("detail")})
    return envelope("verify quote", norms=[], data=data, formulas=formulas, manual_review=manual)
