# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Deterministic state-fee and procedural-deadline calculation.

Every rate, bracket and duration in this module is loaded from
``legal_rules.json``, which carries the version and tariff metadata approved by
colleague. Nothing here hard-codes a rate: the JSON is the single source of
truth, so a tariff change is a new rules file with a new ``version``, never an
edit to this code.

Two design rules come straight from the specification:

* **No guessing.** Missing or inapplicable inputs produce a typed status
  (``MISSING_REQUIRED_PARAM``, ``UNSUPPORTED_CASE_CLASS``,
  ``MANUAL_LEGAL_REVIEW_REQUIRED``) instead of a default value. A calculator
  that silently fills in a legal parameter is worse than one that refuses.
* **Fail closed on unresolved rules.** Where the specification left a question
  open, the behaviour is explicit in the rules file (for example
  ``production_calendar.shift_enabled = false``) and surfaced in the result.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

#: Цена иска: целые рубли (CLI/MCP до v2.3.1) или точная сумма с копейками.
Money = Union[int, Decimal]

RULES_PATH = Path(__file__).with_name("legal_rules.json")

#: Statuses the engine can return.
STATUS_OK = "ok"
STATUS_MISSING_PARAM = "MISSING_REQUIRED_PARAM"
STATUS_UNSUPPORTED_CLASS = "UNSUPPORTED_CASE_CLASS"
STATUS_MANUAL_REVIEW = "MANUAL_LEGAL_REVIEW_REQUIRED"

#: Fee kinds the engine understands.
KIND_PROPERTY = "property_claim"
KIND_COURT_ORDER = "court_order"
KIND_NON_PROPERTY = "non_property_claim"
KIND_APPEAL = "appeal"
KIND_CASSATION_FIRST = "cassation_first"
KIND_CASSATION_SUPREME = "cassation_supreme"
KIND_BANKRUPTCY_PETITION = "bankruptcy_petition"
KIND_BANKRUPTCY_CREDITOR_CLAIM = "bankruptcy_creditor_claim"

_APPLICANT_LABELS = {"individual": "физическое лицо", "entity": "организация"}


class LegalRulesError(Exception):
    """The rules file is missing, unreadable or structurally invalid."""


@dataclass
class FeeResult:
    status: str
    amount: Optional[int] = None
    detail: str = ""
    formula: str = ""
    breakdown: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "amount_rub": self.amount,
            "detail": self.detail,
            "formula": self.formula,
            "breakdown": self.breakdown,
        }


def historical_edition_for(as_of: str, rules: dict) -> Optional[dict]:
    """S1-13: выбрать редакцию тарифов по дате подачи заявления.

    Блок ``historical_court_tariffs`` раньше был мёртвым данными — ни один
    модуль его не читал, а контракт S-41 обещал «дата-тариф». Теперь это
    единственный источник таблиц для дат до текущей редакции (ред. 221-ФЗ
    до 2024-09-08). Пустые редакции (метка текущей) пропускаются: для них
    работают основные ``jisdictions``.
    """
    for ed in (rules.get("historical_court_tariffs") or {}).get("editions", []):
        if not ed.get("jurisdictions"):
            continue
        ef, et = ed.get("effective_from"), ed.get("effective_to")
        if ef and as_of >= ef and (et is None or as_of <= et):
            return ed
    return None


def load_rules(path: Optional[Path] = None) -> dict:
    """Load and validate the declarative rules file."""
    target = Path(path) if path else RULES_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegalRulesError(f"cannot load legal rules: {exc}") from exc
    # S1-15: «jisdictions» — историческая опечатка, зашитая в контракт.
    # Загрузчик принимает оба написания; канонический ключ внутри — «jisdictions»
    # (editions-ветка уже использует правильное «jurisdictions» — отсюда алиас).
    for key in ("version", "tariff_version", "legal_status"):
        if key not in data:
            raise LegalRulesError(f"legal rules missing required key: {key}")
    if "jisdictions" not in data and "jurisdictions" in data:
        data["jisdictions"] = data["jurisdictions"]
    if "jisdictions" not in data:
        raise LegalRulesError("legal rules missing required key: jisdictions")
    return data


def _round_rubles(value: Union[float, Decimal]) -> int:
    """Round to whole rubles per п. 6 ст. 52 НК РФ.

    Below 50 kopeks is dropped; 50 kopeks or more rounds up, which is exactly
    ``floor(value + 0.5)`` for non-negative amounts.

    v2.3.1: the value may already be a ``Decimal`` (a claim price with kopeks).
    ``Decimal + float`` is undefined in Python, so the half is taken in the
    same numeric domain — the rule itself is unchanged and still applied only
    to the *result*, never to the base.
    """
    if isinstance(value, Decimal):
        return math.floor(value + Decimal("0.5"))
    return math.floor(value + 0.5)


def _as_decimal(value) -> Decimal:
    """Exact Decimal for a tariff constant coming from JSON (a float).

    ``Decimal(str(0.007))`` is ``0.007``; ``Decimal(0.007)`` would carry the
    binary artefacts of the literal into a money calculation.
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _progressive(raw: Money, brackets: List[dict], key: str) -> tuple:
    """Evaluate the progressive tariff table.

    Returns ``(amount, formula_text, applied_bracket)``. The first bracket whose
    ``up_to`` covers ``raw`` wins; a ``None`` ``up_to`` is the open-ended final
    bracket. Bracket rates apply to the amount *above* the previous threshold
    except for the first bracket, which is a flat minimum.

    A ``Decimal`` base is kept in Decimal arithmetic end to end: mixing it with
    the float rates from ``legal_rules.json`` would either raise or silently
    reintroduce binary error into a kopeck-precise calculation.
    """
    previous_threshold = 0
    decimal_base = isinstance(raw, Decimal)
    for index, bracket in enumerate(brackets):
        up_to = bracket["up_to"]
        if up_to is None or raw <= up_to:
            base = bracket["base"]
            rate = bracket["rate"]
            if index == 0:
                return base, f"плоская ставка {base:,} руб.", bracket
            excess = raw - previous_threshold
            amount = (_as_decimal(base) + excess * _as_decimal(rate)
                      if decimal_base else base + excess * rate)
            return (
                amount,
                f"{base:,} + {rate * 100:g}% от суммы свыше {previous_threshold:,}",
                bracket,
            )
        previous_threshold = up_to
    raise LegalRulesError("progressive table has no open-ended bracket")


def calculate_fee(
    *,
    claim_amount: Optional[Money] = None,
    jurisdiction: str,
    fee_kind: str,
    applicant_type: Optional[str] = None,
    claim_class: Optional[str] = None,
    rules: Optional[dict] = None,
    as_of: Optional[str] = None,
) -> FeeResult:
    """Calculate one state fee from the approved rules.

    All keyword arguments are explicit: the engine never infers a missing legal
    parameter. In particular ``applicant_type`` is required for every fixed-rate
    fee because the rates differ several-fold between individuals and entities,
    and guessing it would be the single most damaging error this tool could make.

    ``as_of`` (YYYY-MM-DD, дата подачи заявления) переключает таблицы на
    историческую редакцию (S1-13); без неё — действующая редакция.
    """
    rules = rules or load_rules()

    edition: Optional[dict] = None
    if as_of:
        edition = historical_edition_for(as_of, rules)

    jurisdictions_src = rules["jisdictions"] if edition is None \
        else edition["jurisdictions"]
    edition_note = f" [{edition['title']}]" if edition else ""

    if fee_kind not in {
        KIND_PROPERTY,
        KIND_COURT_ORDER,
        KIND_NON_PROPERTY,
        KIND_APPEAL,
        KIND_CASSATION_FIRST,
        KIND_CASSATION_SUPREME,
        KIND_BANKRUPTCY_PETITION,
        KIND_BANKRUPTCY_CREDITOR_CLAIM,
    }:
        return FeeResult(STATUS_MANUAL_REVIEW, detail=f"unknown fee kind: {fee_kind}")

    juris = jurisdictions_src.get(jurisdiction)
    if juris is None:
        return FeeResult(
            STATUS_MANUAL_REVIEW,
            detail=(f"unknown jurisdiction: {jurisdiction}"
                    f"{edition_note or ''}"),
        )

    # Исторические редакции хранят statute, но не label/tariff_article —
    # fallback, чтобы формула ссылалась на норму, а не KeyError (S1-13).
    juris_label = juris.get("label") or jurisdiction
    tariff_article = juris.get("tariff_article") or juris.get("statute") or jurisdiction

    fee_table = juris["fee"]
    spec = fee_table.get(fee_kind)
    if spec is None:
        return FeeResult(
            STATUS_MANUAL_REVIEW,
            detail=(f"fee kind {fee_kind} is not defined for "
                    f"{juris_label}{edition_note}"),
        )

    kind_type = spec["type"]

    if kind_type == "fixed_by_applicant":
        # One check, not two. The membership test alone would also reject None,
        # but then the message blames the value rather than the omission, and
        # the duplicate `if not applicant_type` guard before it made the
        # stricter behaviour untestable (mutation M4 survived).
        if applicant_type not in _APPLICANT_LABELS:
            return FeeResult(
                STATUS_MISSING_PARAM,
                detail=(
                    "MISSING_REQUIRED_PARAM: applicant_type (individual|entity) — "
                    "ставки для физлиц и организаций различаются в разы"
                ),
            )
        amount = spec[applicant_type]
        label = _APPLICANT_LABELS[applicant_type]
        return FeeResult(
            STATUS_OK,
            amount=int(amount),
            detail=f"фиксированная ставка, {label}{edition_note}",
            formula=f"{tariff_article}: {amount:,} руб.{edition_note}",
        )

    # Every remaining kind needs a claim amount. The isinstance check below
    # also rejects None, so this guard exists only to give a clearer message
    # for the common case of an omitted parameter.
    if claim_amount is None:
        return FeeResult(
            STATUS_MISSING_PARAM,
            detail="MISSING_REQUIRED_PARAM: claim_amount",
        )
    if isinstance(claim_amount, bool) or not isinstance(claim_amount, (int, Decimal)):
        return FeeResult(
            STATUS_MISSING_PARAM,
            detail=(
                "MISSING_REQUIRED_PARAM: claim_amount must be an integer or "
                "Decimal number of rubles"
            ),
        )
    if claim_amount <= 0:
        return FeeResult(STATUS_MANUAL_REVIEW, detail="claim_amount must be positive")

    if kind_type == "progressive":
        raw, formula, bracket = _progressive(claim_amount, spec["brackets"], fee_kind)
        # Clamp once, on the rounded amount. Applying the same floor/ceiling to
        # both `raw` and `amount` was redundant and, worse, gave the illusion
        # that a single guard was covered by tests when either line alone would
        # still produce a correct-looking result.
        amount = _round_rubles(raw)
        amount = max(amount, spec["floor"])
        amount = min(amount, spec["ceiling"])
        detail = "прогрессивная шкала"
        if amount == spec["ceiling"] and raw >= spec["ceiling"]:
            detail += ", применён потолок"
        return FeeResult(
            STATUS_OK,
            amount=amount,
            detail=detail + edition_note,
            formula=f"{tariff_article}: {formula}{edition_note}",
            breakdown=[
                {"step": f"исходная сумма иска: {claim_amount:,} руб."},
                {"step": formula},
                {"step": f"после округления (п. 6 ст. 52 НК РФ): {amount:,} руб."},
            ],
        )

    if kind_type == "share":
        source_kind = spec["share_of"]
        base_result = calculate_fee(
            claim_amount=claim_amount,
            jurisdiction=jurisdiction,
            fee_kind=source_kind,
            applicant_type=applicant_type,
            rules=rules,
            as_of=as_of,
        )
        if base_result.status != STATUS_OK:
            return base_result
        share = _round_rubles(base_result.amount * spec["factor"])
        amount = max(share, spec["floor"])
        return FeeResult(
            STATUS_OK,
            amount=int(amount),
            detail=f"{spec['factor'] * 100:g}% от пошлины по имущественному иску{edition_note}",
            formula=(
                f"{base_result.amount:,} × {spec['factor']:g} = {share:,}"
                + (f", применён минимум {spec['floor']:,}" if share < spec["floor"] else "")
            ),
            breakdown=[
                {"step": f"пошлина по имущественному иску: {base_result.amount:,} руб."},
                {"step": f"× {spec['factor'] * 100:g}% = {share:,} руб."},
                {"step": f"минимум {spec['floor']:,} руб." if share < spec["floor"] else "минимум не применялся"},
            ],
        )

    return FeeResult(STATUS_MANUAL_REVIEW, detail=f"unsupported fee type: {kind_type}")


def validate_claim_class(claim_class: Optional[str], rules: Optional[dict] = None, fee_kind: Optional[str] = None) -> FeeResult:
    """Reject case classes the specification excluded from the MVP."""
    rules = rules or load_rules()
    if claim_class is None:
        return FeeResult(
            STATUS_MISSING_PARAM,
            detail="MISSING_REQUIRED_PARAM: claim_class",
        )
    supported_bankruptcy = fee_kind in {
        KIND_BANKRUPTCY_PETITION, KIND_BANKRUPTCY_CREDITOR_CLAIM, KIND_PROPERTY
    }
    if claim_class in rules["unsupported_case_classes"] and not (claim_class == "bankruptcy" and supported_bankruptcy):
        return FeeResult(
            STATUS_UNSUPPORTED_CLASS,
            detail=(
                f"UNSUPPORTED_CASE_CLASS: {claim_class} исключён из MVP "
                "по блоку A спецификации"
            ),
        )
    return FeeResult(STATUS_OK)
