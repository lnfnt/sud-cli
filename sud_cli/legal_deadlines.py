"""Procedural deadline calculation (spec 1.0.0 + addendum 1.1.0).

Addendum 1.1.0 closed Б1–Б4 and changed three things this module implements:

* **Б1** — the working-day counter starts on the next *calendar* day after the
  event, and each non-working day is skipped without incrementing (ч. 3 + ч. 4
  ст. 113 АПК РФ). This is deliberately not "start on the next working day":
  both readings coincide on a Friday event, so the distinction is asserted on a
  mid-week event where they diverge.
* **Б2** — the production calendar is active. Deadlines landing on a non-working
  day are shifted to the next working day (ч. 4 ст. 114 АПК / ч. 2 ст. 108 ГПК).
  Government transfer decrees are encoded for 2026 (№ 1466) and 2027 (№ 1187);
  later years carry a full ``CALENDAR_WARNING``, the encoded years carry a
  source confirmation instead.
* **Б3** — the matrix gained court-order, КАС РФ and ruling-appeal rows.

**Б5** — was open, now RESOLVED by the colleague-legal opinion of 2026-10-08
(``LEGAL-OPINION-B5-MONTH-DEADLINES.md``): a month-long term expires on the
"соответствующее число" of the last month, and that number is the day number of
the **event date** (Day 0), not of the following day (ч. 2 ст. 114 АПК РФ,
ч. 1 ст. 108 ГПК РФ, ч. 1 ст. 92 КАС РФ). The "term starts the next day" rule
only excludes the event day from the count; it does not push the expiry forward.
If the target month has no such day (31 января + 1 месяц), the term expires on
that month's last day. The non-working-day rule is applied *after* this date is
fixed. The switch lives in ``legal_rules.json`` as ``month_calculation_base``;
the rejected ``from_day1`` reading raises instead of computing silently.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .legal_engine import (
    STATUS_MANUAL_REVIEW,
    STATUS_MISSING_PARAM,
    STATUS_OK,
    LegalRulesError,
    load_rules,
)
from .production_calendar import (
    add_calendar_days,
    add_working_days,
    calendar_warnings,
    is_working_day,
)


@dataclass
class DeadlineResult:
    status: str
    deadline: Optional[date] = None
    detail: str = ""
    legal_basis: str = ""
    warnings: List[str] = field(default_factory=list)
    trace: List[dict] = field(default_factory=list)

    deadline_datetime: Optional[str] = None
    time_with_tz: Optional[str] = None
    region: Optional[str] = None

    def to_dict(self) -> dict:
        payload = {
            "status": self.status,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "detail": self.detail,
            "legal_basis": self.legal_basis,
            "warnings": self.warnings,
            "trace": self.trace,
        }
        if self.deadline_datetime is not None:
            payload.update({"deadline_datetime": self.deadline_datetime, "time_with_tz": self.time_with_tz})
        if self.region is not None:
            payload["region"] = self.region
        return payload


def _shift_to_working_day(day: date, trace: List[dict], warnings: List[str], region=None) -> date:
    """Move a deadline off a non-working day (ч. 4 ст. 114 АПК / ч. 2 ст. 108 ГПК)."""
    if is_working_day(day, region=region):
        return day
    original = day
    while not is_working_day(day, region=region):
        day += timedelta(days=1)
    trace.append(
        {
            "step": (
                f"срок истекал {original.isoformat()} — нерабочий день, "
                f"перенесён на {day.isoformat()}"
            )
        }
    )
    warnings.append(
        f"Расчётная дата {original.isoformat()} приходилась на нерабочий день; "
        f"срок перенесён на {day.isoformat()} (ч. 4 ст. 114 АПК РФ / ч. 2 ст. 108 ГПК РФ)."
    )
    return day


def _add_months(start: date, months: int) -> date:
    """Add calendar months, clamping to the last day of a shorter month.

    Per ст. 192 ГК РФ a term set in months ends on the corresponding day of
    the last month, so ``30 сентября + 1 месяц -> 30 октября``. Only when the
    target month has no such day does the term end on that month's last day:
    ``31 января + 1 месяц -> 28/29 февраля``.

    The earlier version branched on ``start.day > 28``, which sent every date
    from the 29th onward to the month's last day — so 30 September became
    31 October and 30 April became 31 May, each a day late.
    """
    total_month = start.month - 1 + months
    year = start.year + total_month // 12
    month = total_month % 12 + 1
    next_month_first = date(year + (month == 12), (month % 12) + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


def calculate_deadline(
    *,
    deadline_key: str,
    event_date: Optional[date] = None,
    rules: Optional[dict] = None,
    region: Optional[str] = None,
    tz: Optional[str] = None,
) -> DeadlineResult:
    """Calculate one procedural deadline from the approved matrix."""
    rules = rules or load_rules()
    if region is not None:
        try:
            from .regional_holidays import normalize_region
            region = normalize_region(region)
        except ValueError as exc:
            return DeadlineResult(STATUS_MANUAL_REVIEW, detail=str(exc))
    zone = None
    if tz is not None:
        try:
            zone = ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            return DeadlineResult(STATUS_MANUAL_REVIEW, detail=f"unknown timezone: {tz}")
    matrix = rules.get("deadline_matrix", {})

    entry = matrix.get(deadline_key)
    if entry is None:
        return DeadlineResult(
            STATUS_MANUAL_REVIEW,
            detail=(
                f"срок '{deadline_key}' отсутствует в матрице спецификации; "
                "возможен нестандартный срок — требуется ручная проверка"
            ),
        )

    if entry.get("always_manual_review"):
        return DeadlineResult(
            STATUS_MANUAL_REVIEW,
            detail=(
                "специальные сокращённые сроки (напр. по обеспечению иска) "
                "в MVP не кодируются — требуется ручная проверка"
            ),
        )

    duration = entry.get("duration")
    if not duration:
        return DeadlineResult(
            STATUS_MANUAL_REVIEW,
            detail=f"у срока '{deadline_key}' не задан срок — требуется ручная проверка",
        )

    if not event_date:
        return DeadlineResult(
            STATUS_MISSING_PARAM,
            detail=f"MISSING_REQUIRED_PARAM: event_date (триггер: {entry['trigger']})",
        )

    juris_code = entry["jurisdiction"]
    juris = rules["jisdictions"].get(juris_code)
    if juris is None:
        raise LegalRulesError(f"matrix references unknown jurisdiction: {juris_code}")

    day_mode = juris["deadline_day_mode"]
    warnings: List[str] = []
    trace: List[dict] = [
        {"step": f"событие (Day 0): {event_date.isoformat()}, не засчитывается"}
    ]

    # Day 0 is never counted (п. 6): the term starts the next calendar day.
    day1 = event_date + timedelta(days=1)
    trace.append({"step": f"Day 1 = {day1.isoformat()} (следующий календарный день)"})

    unit = duration["unit"]
    b5_open = rules["spec_open_questions"].get("B5_monthly_base", "").startswith("UNRESOLVED")

    if unit == "months":
        # Б5 RESOLVED (colleague-legal, 2026-10-08, ч.2 ст.114 АПК РФ): a term counted in
        # months expires on the corresponding day of the last month, and that
        # "соответствующее число" is the day number of the EVENT date (Day 0).
        # The "term starts the next day" rule (ч.4 ст.113 АПК РФ) only excludes the
        # event day itself from the count — it does not push the expiry forward,
        # otherwise the party would get one month plus a day.
        base = rules.get("month_calculation_base", "event_date")
        if base == "event_date":
            deadline = _add_months(event_date, duration["value"])
            trace.append(
                {
                    "step": (
                        f"+{duration['value']} мес. от даты события "
                        f"{event_date.isoformat()} по правилу «число в число» "
                        "(ч.2 ст.114 АПК РФ)"
                    )
                }
            )
            trace.append(
                {
                    "step": (
                        "день события не засчитывается, но число окончания "
                        "не сдвигается на следующий день"
                    )
                }
            )
        elif base == "from_day1":
            # Disabled by the Б5 opinion: explicitly retained only so that a
            # mis-set switch fails loudly instead of silently reverting to the
            # rejected reading. Never selected by default.
            raise LegalRulesError(
                "month_calculation_base='from_day1' признан некорректным "
                "заключением colleague-legal от 2026-10-08 (Б5 закрыт): допустимо "
                "только 'event_date'"
            )
        else:
            raise LegalRulesError(
                f"unknown month_calculation_base: {base!r} "
                "(допустимо только 'event_date')"
            )
    elif unit == "working_days":
        # Б1: the counter starts at the next calendar day and skips non-working
        # days without incrementing, so day1 is passed through unchanged.
        deadline = add_working_days(day1, duration["value"], region=region)
        trace.append(
            {"step": f"+{duration['value']} рабочих дней (выходные и праздники пропущены)"}
        )
    elif unit == "calendar_days":
        deadline = add_calendar_days(day1, duration["value"])
        trace.append({"step": f"+{duration['value']} календарных дней"})
    else:
        raise LegalRulesError(f"unknown duration unit: {unit}")

    deadline = _shift_to_working_day(deadline, trace, warnings, region)

    warnings.extend(calendar_warnings(deadline.year, region=region))

    if b5_open:
        # Legacy branch: only reachable if the rules file is rolled back to the
        # pre-opinion state. Kept so an old rules file fails with an explanation
        # rather than silently computing on the rejected reading.
        warnings.append(
            "Б5 (открыт): для месячных сроков база отсчёта не подтверждена — "
            "расчёт следует п.6 (отсчёт от дня, следующего за событием). "
            "Пример п.7 спеки («31 января → 28 февраля») предполагает базу от даты "
            "события и дал бы другой результат."
        )

    warnings.append(rules["boundary_note"])

    result = DeadlineResult(
        status=STATUS_OK,
        deadline=deadline,
        detail=f"{day_mode}, триггер: {entry['trigger']}",
        legal_basis=entry["legal_basis"],
        warnings=warnings,
        trace=trace,
        region=region,
    )
    if zone is not None:
        local = datetime.combine(deadline, time(23, 59, 59), tzinfo=zone)
        result.deadline_datetime = local.isoformat()
        result.time_with_tz = f"{local:%Y-%m-%d %H:%M:%S} {local.tzname()} ({tz})"
    return result
