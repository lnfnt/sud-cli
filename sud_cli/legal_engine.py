"""Deterministic state-fee and procedural-deadline calculation.

Every rate, bracket and duration in this module is loaded from
``legal_rules.json``, which carries the version and tariff metadata approved by
colleague-legal. Nothing here hard-codes a rate: the JSON is the single source of
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
from datetime import date, datetime, timedelta
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
    #: Tariff provenance (S-81). Filled only for an explicit ``as_of`` call.
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        document = {
            "status": self.status,
            "amount_rub": self.amount,
            "detail": self.detail,
            "formula": self.formula,
            "breakdown": self.breakdown,
        }
        # ``meta`` is emitted only when it carries something. An ``as_of``-free
        # call therefore returns the byte-for-byte pre-2.10.0 envelope: adding a
        # field to the class must not silently change the published contract
        # that CLI/MCP consumers pinned (task-briefd §1, backward-compat).
        if self.meta:
            document["meta"] = dict(self.meta)
        return document


def load_rules(path: Optional[Path] = None) -> dict:
    """Load and validate the declarative rules file."""
    target = Path(path) if path else RULES_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegalRulesError(f"cannot load legal rules: {exc}") from exc
    for key in ("version", "tariff_version", "legal_status", "jisdictions"):
        if key not in data:
            raise LegalRulesError(f"legal rules missing required key: {key}")
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


#: Warning emitted when ``as_of`` predates the earliest VERIFIED tariff edition.
#: The engine answers with the acting scale and says so out loud — inventing an
#: unverified pre-2015 rate is the failure mode this guard exists to prevent.
AS_OF_PRE_VERIFIED_WARNING = (
    "as_of раньше самой ранней VERIFIED редакции — расчёт по текущему тарифу, "
    "не по устаревшему догадочному"
)

AS_OF_OUTSIDE_WINDOWS_WARNING = (
    "as_of не попадает ни в одно окно VERIFIED тарифных редакций — расчёт по "
    "текущему тарифу"
)


def parse_as_of(value) -> date:
    """Strict ``as_of`` coercion: date/datetime or ISO ``YYYY-MM-DD`` string.

    The accepted text form is the zero-padded calendar ISO shape only: the parse
    is round-tripped, so ``2020-6-1``, ``2020-13-01``, ``31.12.2020`` and
    ``20200601`` are all refused. An ambiguous date must never silently select a
    tariff edition — a fee computed against the wrong edition is a wrong fee.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(
                f"as_of '{value}' не в формате ГГГГ-ММ-ДД"
            ) from None
        if parsed.isoformat() != text:
            raise ValueError(f"as_of '{value}' не в формате ГГГГ-ММ-ДД")
        return parsed
    raise ValueError(
        f"as_of must be a date or an ISO 'YYYY-MM-DD' string, got {type(value).__name__}"
    )


def tariff_editions(rules: dict) -> List[dict]:
    """The VERIFIED historical tariff editions shipped inside ``legal_rules.json``."""
    pack = rules.get("historical_court_tariffs") or {}
    return pack.get("editions") or []


def _edition_start(edition: dict) -> date:
    return parse_as_of(edition["effective_from"])


def _edition_end(edition: dict) -> Optional[date]:
    raw = edition.get("effective_to")
    return None if raw in (None, "") else parse_as_of(raw)


def acting_edition(rules: dict) -> dict:
    """The edition that carries no closing date (or the file-level tariff basis).

    Used both as the default answer and as the fallback target for an ``as_of``
    outside every VERIFIED window, so provenance is never blank. When the pack
    declares more than one open window, the newest one is the acting tariff.
    """
    open_windows = [e for e in tariff_editions(rules) if _edition_end(e) is None]
    if open_windows:
        return max(open_windows, key=_edition_start)
    basis = rules.get("tariff_basis") or {}
    return {
        "edition_id": rules.get("tariff_version"),
        "effective_from": basis.get("effective_from"),
        "effective_to": None,
        "basis": basis.get("federal_law"),
    }


def select_tariff_edition(rules: dict, as_of: date) -> tuple:
    """Resolve the edition in force on ``as_of``; boundaries are inclusive.

    Returns ``(edition_or_None, warnings)``. ``None`` means "fall back to the
    acting (current) tariff base", always paired with a warning string so the
    substitution is visible in the answer instead of silent.

    The latest matching window wins: a pack listed out of order, or a later
    edition that narrows an earlier one, must not silently select the older
    scale.
    """
    editions = tariff_editions(rules)
    if not editions:
        return None, [AS_OF_OUTSIDE_WINDOWS_WARNING]
    matches = []
    for edition in editions:
        start = _edition_start(edition)
        end = _edition_end(edition)
        if as_of >= start and (end is None or as_of <= end):
            matches.append(edition)
    if matches:
        return max(matches, key=_edition_start), []
    earliest = min(_edition_start(e) for e in editions)
    if as_of < earliest:
        return None, [AS_OF_PRE_VERIFIED_WARNING]
    return None, [AS_OF_OUTSIDE_WINDOWS_WARNING]


def tariff_provenance(rules: dict, edition: Optional[dict], as_of: date,
                     warnings: List[str]) -> dict:
    """Provenance block for one as-of result: which edition produced the number."""
    applied = edition or acting_edition(rules)
    scale = ("historical_court_tariffs"
             if edition and edition.get("jurisdictions") else "current_tariff_base")
    meta = {
        "as_of": as_of.isoformat(),
        "edition_id": applied.get("edition_id"),
        "effective_from": applied.get("effective_from"),
        "effective_to": applied.get("effective_to"),
        "basis": applied.get("basis"),
        "tariff_effective_window": (
            # A closed window reads ``2015-01-01..2024-09-08``; the acting
            # edition has no closing date, and says so instead of pretending
            # the range ended: ``2024-09-09..без даты окончания``.
            f"{applied.get('effective_from')}.."
            f"{applied.get('effective_to') or 'без даты окончания'}"
        ),
        "tariff_scale": scale,
    }
    if warnings:
        meta["warnings"] = list(warnings)
    return meta


def _eval_progressive(raw: Money, brackets: List[dict]) -> tuple:
    """Evaluate a progressive tariff table — the ONE helper used by every scale.

    Returns ``(amount, formula_text, applied_bracket)``. The first bracket whose
    ``up_to`` covers ``raw`` wins; a ``None`` ``up_to`` is the open-ended final
    bracket. Each bracket is read literally from the rules JSON:

    * ``rate_on: "threshold"`` — flat ``base`` for the whole band (the acting
      259-ФЗ scale opens with such a minimum);
    * ``rate_on: "full"`` — ``rate`` applied to the whole claim amount
      (221-ФЗ: «4% от суммы»);
    * ``rate_on: "over"`` — ``base`` plus ``rate`` on the part above the previous
      bracket's ``up_to`` (221-ФЗ: «4 000 + 3% от суммы свыше 100 000»).

    Both readings of this data agree because every later ``base`` already equals
    the sum of the earlier bands; the piecewise sum is therefore identical to the
    single-bracket formula, and this function keeps exactly one implementation of
    it (task-briefd §1) so the two scales cannot drift apart.

    ``floor``/``ceiling`` are deliberately NOT applied here: the caller clamps
    once, after the table is evaluated, so a per-bracket minimum can never be
    mistaken for the edition's minimum.

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
            rate_on = bracket.get("rate_on") or ("threshold" if index == 0 else "over")
            if rate_on == "threshold":
                return base, f"плоская ставка {base:,} руб.", bracket
            excess = raw if rate_on == "full" else raw - previous_threshold
            amount = (_as_decimal(base) + _as_decimal(rate) * excess
                      if decimal_base else base + excess * rate)
            if rate_on == "full":
                formula = (f"{rate * 100:g}% от всей суммы" if not base else
                           f"{base:,} + {rate * 100:g}% от всей суммы")
            else:
                formula = (f"{base:,} + {rate * 100:g}% от суммы свыше "
                           f"{previous_threshold:,}")
            return amount, formula, bracket
        previous_threshold = up_to
    raise LegalRulesError("progressive table has no open-ended bracket")


def _juris_view(juris: dict, base_entry: Optional[dict]) -> tuple:
    """``(label, tariff_article)`` for a fee table of any provenance.

    The current base keys its blocks ``label``/``tariff_article``; the VERIFIED
    historical pack keys the same thing ``statute`` and carries no label. Reading
    them through one accessor pair keeps every formula string honest without
    inventing text: for the current path the values are byte-identical to the
    previous direct ``juris['...']`` lookups.
    """
    article = juris.get("tariff_article") or juris.get("statute") or ""
    label = (juris.get("label")
             or (base_entry or {}).get("label")
             or "суд")
    return label, article


def calculate_fee(
    *,
    claim_amount: Optional[Money] = None,
    jurisdiction: str,
    fee_kind: str,
    applicant_type: Optional[str] = None,
    claim_class: Optional[str] = None,
    rules: Optional[dict] = None,
    as_of=None,
) -> FeeResult:
    """Calculate one state fee from the approved rules.

    All keyword arguments are explicit: the engine never infers a missing legal
    parameter. In particular ``applicant_type`` is required for every fixed-rate
    fee because the rates differ several-fold between individuals and entities,
    and guessing it would be the single most damaging error this tool could make.

    ``as_of`` (v2.10.0, S-41) dates the tariff: a ``date`` or an ISO
    ``YYYY-MM-DD`` string selects the VERIFIED edition whose window covers it,
    boundaries inclusive. Leaving it ``None`` is *not* an invisible default
    edition — it is the pre-2.10.0 contract: the acting tariff, no provenance
    block, byte-identical envelope. An ``as_of`` outside every VERIFIED window
    answers with the acting tariff plus a warning instead of guessing an
    unverified historical rate.
    """
    rules = rules or load_rules()

    as_of_date = parse_as_of(as_of) if as_of is not None else None
    edition = None
    warnings: List[str] = []
    if as_of_date is not None:
        edition, warnings = select_tariff_edition(rules, as_of_date)
        if edition is None and warnings and AS_OF_PRE_VERIFIED_WARNING in warnings:
            # Акт legal п.3 (VERIFIED): дата раньше первой зафиксированной
            # редакции — расчёт НЕ выдаётся, manual_review. Подмена текущим
            # тарифом под исторической датой юридически неверна.
            return FeeResult(
                STATUS_MANUAL_REVIEW,
                detail="as_of раньше первой VERIFIED тарифной редакции "
                       "(2015-01-01): историческая шкала не подтверждена — "
                       "нужен точечный акт colleague-legal; текущий тариф под "
                       "исторической датой не подставляется",
                meta={"as_of": as_of_date.isoformat(),
                      "manual_reason": "pre_verified_tariff_date"},
            )
    provenance = (tariff_provenance(rules, edition, as_of_date, warnings)
                  if as_of_date is not None else {})

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
        return FeeResult(STATUS_MANUAL_REVIEW, detail=f"unknown fee kind: {fee_kind}",
                         meta=provenance)

    # ``as_of`` selects the tariff tables. An edition that ships its own
    # ``jurisdictions`` block (the 221-ФЗ pack) answers from it; an edition that
    # only carries metadata (the acting 259-ФЗ entry) means "the tables in this
    # file". Falling back is never silent: the provenance block names which one
    # produced the number.
    edition_tables = edition.get("jurisdictions") if edition else None
    edition_id = edition.get("edition_id") if edition else None
    base_tables = rules["jisdictions"]
    if edition_tables is not None:
        juris = edition_tables.get(jurisdiction)
        if juris is None:
            return FeeResult(
                STATUS_MANUAL_REVIEW,
                detail=(
                    f"MANUAL_LEGAL_REVIEW_REQUIRED: редакция "
                    f"'{edition_id}' не содержит VERIFIED тарифа "
                    f"для юрисдикции '{jurisdiction}' — текущий тариф под такую "
                    "историческую дату не подставляется"
                ),
                meta=provenance,
            )
    else:
        juris = base_tables.get(jurisdiction)
        if juris is None:
            return FeeResult(STATUS_MANUAL_REVIEW, detail=f"unknown jurisdiction: {jurisdiction}")

    label, article = _juris_view(juris, base_tables.get(jurisdiction))
    fee_table = juris["fee"]
    spec = fee_table.get(fee_kind)
    if spec is None:
        return FeeResult(
            STATUS_MANUAL_REVIEW,
            detail=f"fee kind {fee_kind} is not defined for {label}",
            meta=provenance,
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
                meta=provenance,
            )
        amount = spec[applicant_type]
        applicant_label = _APPLICANT_LABELS[applicant_type]
        return FeeResult(
            STATUS_OK,
            amount=int(amount),
            detail=f"фиксированная ставка, {applicant_label}",
            formula=f"{article}: {amount:,} руб.",
            meta=provenance,
        )

    # A flat rate is flat: one value from the edition, no applicant split and no
    # claim amount. This shape exists only in the historical pack (221-ФЗ appeal
    # and cassation fees), which is why it is read straight from the edition.
    if kind_type == "fixed":
        amount = int(spec["amount"])
        breakdown = []
        if spec.get("note"):
            breakdown.append({"step": f"основание ставки: {spec['note']}"})
        return FeeResult(
            STATUS_OK,
            amount=amount,
            detail="фиксированная ставка редакции",
            formula=f"{article}: {amount:,} руб.",
            breakdown=breakdown,
            meta=provenance,
        )

    # Every remaining kind needs a claim amount. The isinstance check below
    # also rejects None, so this guard exists only to give a clearer message
    # for the common case of an omitted parameter.
    if claim_amount is None:
        return FeeResult(
            STATUS_MISSING_PARAM,
            detail="MISSING_REQUIRED_PARAM: claim_amount",
            meta=provenance,
        )
    if isinstance(claim_amount, bool) or not isinstance(claim_amount, (int, Decimal)):
        return FeeResult(
            STATUS_MISSING_PARAM,
            detail=(
                "MISSING_REQUIRED_PARAM: claim_amount must be an integer or "
                "Decimal number of rubles"
            ),
            meta=provenance,
        )
    if claim_amount <= 0:
        return FeeResult(STATUS_MANUAL_REVIEW, detail="claim_amount must be positive",
                         meta=provenance)

    if kind_type == "progressive":
        raw, formula, bracket = _eval_progressive(claim_amount, spec["brackets"])
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
            detail=detail,
            formula=f"{article}: {formula}",
            breakdown=[
                {"step": f"исходная сумма иска: {claim_amount:,} руб."},
                {"step": formula},
                {"step": f"после округления (п. 6 ст. 52 НК РФ): {amount:,} руб."},
            ],
            meta=provenance,
        )

    if kind_type == "share":
        source_kind = spec["share_of"]
        base_result = calculate_fee(
            claim_amount=claim_amount,
            jurisdiction=jurisdiction,
            fee_kind=source_kind,
            applicant_type=applicant_type,
            rules=rules,
            as_of=as_of_date,
        )
        if base_result.status != STATUS_OK:
            return base_result
        share = _round_rubles(base_result.amount * spec["factor"])
        amount = max(share, spec["floor"])
        return FeeResult(
            STATUS_OK,
            amount=int(amount),
            detail=f"{spec['factor'] * 100:g}% от пошлины по имущественному иску",
            formula=(
                f"{base_result.amount:,} × {spec['factor']:g} = {share:,}"
                + (f", применён минимум {spec['floor']:,}" if share < spec["floor"] else "")
            ),
            breakdown=[
                {"step": f"пошлина по имущественному иску: {base_result.amount:,} руб."},
                {"step": f"× {spec['factor'] * 100:g}% = {share:,} руб."},
                {"step": f"минимум {spec['floor']:,} руб." if share < spec["floor"] else "минимум не применялся"},
            ],
            meta=provenance,
        )

    return FeeResult(STATUS_MANUAL_REVIEW, detail=f"unsupported fee type: {kind_type}",
                     meta=provenance)


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
