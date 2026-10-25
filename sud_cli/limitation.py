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


def calculate_limitation(*, start: date, period_type: str = "general",
                         claim_notice_days: int = 0, check_date: date | None = None,
                         custom_years: int = 0, custom_months: int = 0) -> LimitationResult:
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

    expiry = _add_months(start, years * 12 + months)
    trace = [{"step": f"начало срока: {start.isoformat()} (ст. 200 ГК РФ)"}]
    if years or months:
        trace.append({"step": f"добавлен срок: {years} лет, {months} мес."})
    if claim_notice_days:
        expiry += timedelta(days=claim_notice_days)
        basis += "; п. 3 ст. 202 ГК РФ"
        trace.append({"step": f"досудебный порядок: +{claim_notice_days} календарных дней"})
    while not is_working_day(expiry):
        expiry += timedelta(days=1)
    trace.append({"step": f"дата окончания после переноса: {expiry.isoformat()} (ст. 193 ГК РФ)"})
    expired = check_date > expiry
    delta = (check_date - expiry).days
    return LimitationResult(
        "ok", expired, expiry.isoformat(), max(0, -delta), max(0, delta),
        [basis, "ст. 191 ГК РФ", "ст. 193 ГК РФ"], trace,
    )
