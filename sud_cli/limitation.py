# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Calculation of civil limitation periods (ст. 196, 200, 202 ГК РФ)."""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta

from .production_calendar import is_working_day


PERIODS = {
    "general": (3, 0, "ст. 196, 200 ГК РФ"),
    "contract_work": (1, 0, "ст. 725 ГК РФ"),
    "carriage": (1, 0, "ст. 797 ГК РФ"),
    "voidable_deal": (1, 0, "ст. 181 ГК РФ"),
}


@dataclass
class LimitationResult:
    status: str
    is_expired: bool = False
    expiry_date: str | None = None
    days_left: int = 0
    days_overdue: int = 0
    legal_basis: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    detail: str = ""

    def to_dict(self):
        return self.__dict__.copy()


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _months_between(early: date, late: date) -> int:
    """Полных календарных месяцев от early до late (остаток срока)."""
    months = (late.year - early.year) * 12 + (late.month - early.month)
    if late.day < early.day:
        months -= 1
    return months


def calculate_limitation(*, start: date, period_type: str = "general",
                         claim_notice_days: int = 0, check_date: date | None = None,
                         custom_years: int = 0, custom_months: int = 0,
                         pretrial_start: date | None = None,
                         pretrial_end: date | None = None) -> LimitationResult:
    """Срок исковой давности.

    W6/S1-3 (заключение colleague): досудебный порядок ПРИОСТАНАВЛИВАЕТ срок
    (п. 3 ст. 202 ГК РФ; п. 16 ППВС № 43 от 29.09.2015) — а не «добавляет дни
    к концу». Модель: итог = конец порядка + остаток срока на дату его начала;
    если остаток менее 6 месяцев (или порядок начат за пределами срока) —
    продление до 6 месяцев с конца порядка (п. 4 ст. 202 ГК РФ).

    ``claim_notice_days`` — легаси-путь (аддитивная модель) сохранён для
    обратной совместимости; новые вызовы обязаны передавать даты
    ``pretrial_start``/``pretrial_end``.
    """
    if check_date is None:
        check_date = date.today()
    if period_type in PERIODS:
        years, months, basis = PERIODS[period_type]
    elif period_type == "custom_years" and custom_years > 0:
        years, months, basis = custom_years, 0, "ст. 196 ГК РФ (индивидуальный срок)"
    elif period_type == "custom_months" and custom_months > 0:
        years, months, basis = 0, custom_months, "ст. 192 ГК РФ (индивидуальный срок)"
    else:
        return LimitationResult("error", detail="unknown period type or non-positive custom duration")

    if (pretrial_start is None) != (pretrial_end is None):
        return LimitationResult(
            "error", detail="INVALID_ARGUMENTS: pretrial_start и pretrial_end "
                            "передаются только вместе (даты начала и окончания "
                            "досудебного порядка)")
    ps, pe = pretrial_start, pretrial_end

    expiry = _add_months(start, years * 12 + months)
    trace = [{"step": f"начало срока: {start.isoformat()} (ст. 200 ГК РФ)"}]
    if years or months:
        trace.append({"step": f"добавлен срок: {years} лет, {months} мес."})

    if ps is not None and pe is not None:
        if ps < start:
            return LimitationResult(
                "error", detail="INVALID_ARGUMENTS: дата начала досудебного "
                                "порядка раньше начала течения срока")
        if pe <= ps:
            return LimitationResult(
                "error", detail="INVALID_ARGUMENTS: дата окончания досудебного "
                                "порядка не позже даты его начала")
        remainder = _months_between(ps, expiry)
        trace.append({"step": f"досудебный порядок: {ps.isoformat()} "
                              f"— {pe.isoformat()}"})
        if ps >= expiry or remainder < 6:
            # п. 4 ст. 202 ГК РФ: до истечения менее 6 месяцев либо порядок
            # начат за пределами срока — продление до 6 месяцев с конца.
            expiry = _add_months(pe, 6)
            basis += "; п. 4 ст. 202 ГК РФ"
            trace.append({"step": "остаток срока на начало порядка: менее 6 мес. "
                                  "или срок истёк — продление до 6 месяцев "
                                  "с момента окончания порядка (п. 4 ст. 202 ГК РФ)"})
        else:
            expiry = _add_months(pe, remainder)
            basis += "; п. 3 ст. 202 ГК РФ; п. 16 Постановления Пленума ВС РФ от 29.09.2015 № 43"
            trace.append({"step": f"остаток срока на дату начала порядка: {remainder} мес. — "
                                  f"приостановление, итог = конец порядка + остаток"})
        trace.append({"step": f"итоговая дата до переноса: {expiry.isoformat()}"})
    elif claim_notice_days:
        # Легаси-аддитивная модель — только для старых вызовов без дат.
        expiry += timedelta(days=claim_notice_days)
        basis += "; п. 3 ст. 202 ГК РФ"
        trace.append({"step": f"досудебный порядок: +{claim_notice_days} календарных дней "
                              f"(легаси-модель, передавайте даты порядка)"})
    while not is_working_day(expiry):
        expiry += timedelta(days=1)
    trace.append({"step": f"дата окончания после переноса: {expiry.isoformat()} (ст. 193 ГК РФ)"})
    expired = check_date > expiry
    delta = (check_date - expiry).days
    return LimitationResult(
        "ok", expired, expiry.isoformat(), max(0, -delta), max(0, delta),
        [basis, "ст. 191 ГК РФ", "ст. 193 ГК РФ"], trace,
    )
