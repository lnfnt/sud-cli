"""Production calendar for federal deadlines (addendum 1.1.0, п. Б2).

The calendar carries three layers, and each is recorded with its own legal
source so a reader can tell how much of the answer is statute and how much is
decree:

* **ст. 112 ТК РФ** — the fixed federal non-working holidays. These repeat every
  year, so a year with no calendar entry still gets them.
* **ч. 2 ст. 112 ТК РФ** — automatic moves. A holiday landing on a weekend moves
  to the next working day on its own, except for the holidays named in абз. 2
  and 3 (1–8 января, 23 февраля), where only the Government may move the day off.
* **Постановления Правительства РФ** — the yearly transfer decrees.

The transfer decrees used to be absent here: they are published per-year and no
verified copy was supplied, and transcribing a calendar from memory would be a
silent legal guess inside a deadline calculator. The 2026 decree is now encoded
because it was verified against the official portal scan (№ 1466 от 24.09.2025,
publication N 0001202509240023). 2026 is the only verified year, so the
``CALENDAR_WARNING`` is now **per-year** rather than global: a deadline landing in
a year whose decree is encoded carries a note saying so, and every other year
still carries the full warning. Failing a year loudly is the point — the tool
says what it does not know instead of pretending.
"""

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set

from .regional_holidays import holidays_for_year as regional_holidays, normalize_region, region_name

CALENDAR_PATH = Path(__file__).with_name("holidays_rf.json")
TRANSFERS_PATH = Path(__file__).with_name("government_transfers.json")

REGIONAL_NOTE = (
    "Региональные нерабочие праздничные дни субъектов РФ не учитываются."
)


class CalendarError(Exception):
    """The calendar file is missing or structurally invalid."""


@lru_cache(maxsize=1)
def load_calendar(path: Optional[str] = None) -> dict:
    target = Path(path) if path else CALENDAR_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarError(f"cannot load production calendar: {exc}") from exc
    if "federal_holidays" not in data:
        raise CalendarError("production calendar missing federal_holidays")
    return data


def _parse(token: str, year: int) -> date:
    month, day = (int(part) for part in token.split("-"))
    return date(year, month, day)


@lru_cache(maxsize=1)
def _holiday_index(calendar_path: Optional[str] = None) -> Dict[int, Set[date]]:
    data = load_calendar(calendar_path)
    index: Dict[int, Set[date]] = {}
    for year, days in data["federal_holidays"].items():
        index[int(year)] = {_parse(token, int(year)) for token in days}
    return index


def _statutory_pattern(calendar_path: Optional[str] = None) -> List[str]:
    """The ст. 112 ТК РФ day pattern, taken from any populated year.

    The statutory dates repeat annually, so they are used as the fallback for a
    year that has no calendar entry. Only the fixed holidays are used; the
    automatic weekend moves are applied separately by ``holidays_for_year``.
    """
    data = load_calendar(calendar_path)
    for days in data["federal_holidays"].values():
        return list(days)
    raise CalendarError("production calendar has no years to copy the pattern from")


def holidays_for_year(year: int, calendar_path: Optional[str] = None, region=None) -> Set[date]:
    """Every federal non-working day in ``year``.

    This is the union of the ст. 112 ТК РФ holidays and any day the Government
    declared a day off by decree. Days moved *out* of a holiday by a decree are
    not representable in a set of non-working days; that is why
    ``days_restored_to_working`` is applied by :func:`is_working_day` rather
    than here.
    """
    index = _holiday_index(calendar_path)
    base = index.get(year) or {_parse(token, year) for token in _statutory_pattern(calendar_path)}
    transfer = load_calendar(calendar_path).get("transfers", {}).get(str(year), {})
    # ч. 2 ст. 112 ТК РФ: a holiday landing on a weekend moves to the next working
    # day on its own. These are derived from the statute, not from the decree, and
    # are recorded per-year because the set depends on which weekdays that year
    # happens to start on.
    cases = load_calendar(calendar_path).get("auto_transfers_st112", {}).get(str(year), [])
    for case in cases:
        base.add(_parse(case["moved_to"], year))
    # v2.3.2 (sa-1 F-12): ч.2 ст.112 переносы выводятся алгоритмически для ЛЮБОГО
    # года, а не только записанных (23.02.2025 воскресенье -> 24.02.2025 отсутствовал).
    # Явные записи выше имеют приоритет: праздник, уже перенесённый записью, не
    # сдвигается повторно. Не переносим: январский блок (каникулы поглощают перенос,
    # позиция Роструда) и даты вне фиксированного statutory-списка (декретные дни).
    _ST112_FIXED = {"02-23", "03-08", "05-01", "05-09", "06-12", "11-04"}
    _explicit_holidays = {case.get("holiday") for case in cases}
    derived: Set[date] = set()
    for token in _statutory_pattern(calendar_path):
        if token not in _ST112_FIXED or token in _explicit_holidays:
            continue
        h = _parse(token, year)
        if h.weekday() < 5:
            continue
        cand = h + timedelta(days=1)
        while cand.weekday() >= 5 or cand in base or cand in derived:
            cand += timedelta(days=1)
        derived.add(cand)
    base |= derived
    return (base | {_parse(token, year) for token in transfer.get("extra_days_off", [])} |
            regional_holidays(year, region))


def days_restored_to_working(year: int, calendar_path: Optional[str] = None) -> Set[date]:
    """Days a decree moved a day off back onto a working day."""
    return {
        _parse(token, year)
        for token in (
            load_calendar(calendar_path)
            .get("transfers", {})
            .get(str(year), {})
            .get("days_restored_to_working", [])
        )
    }


def is_working_day(day: date, calendar_path: Optional[str] = None, region=None) -> bool:
    """True when ``day`` counts as a working day for АПК/ГПК deadlines."""
    if day in days_restored_to_working(day.year, calendar_path):
        return True
    if day.weekday() >= 5:
        return False
    return day not in holidays_for_year(day.year, calendar_path, region)


def auto_transfers_encoded(calendar_path: Optional[str] = None) -> bool:
    """Whether ч. 2 ст. 112 ТК РФ automatic moves are encoded for ``year``."""
    return bool(load_calendar(calendar_path).get("auto_transfers_st112"))


def transfers_encoded_for(year: int, calendar_path: Optional[str] = None) -> bool:
    """Whether the Government transfer decree for ``year`` is actually encoded.

    Per-year on purpose: encoding 2026 must not silence the warning for 2027.
    """
    data = load_calendar(calendar_path)
    verified = data.get("years_with_verified_transfers", [])
    return year in verified or str(year) in verified


def calendar_warnings(year: int, calendar_path: Optional[str] = None, region=None) -> List[str]:
    """Warnings that must accompany any deadline computed for ``year``."""
    data = load_calendar(calendar_path)
    warnings: List[str] = []
    if transfers_encoded_for(year, calendar_path):
        decree = data.get("transfers", {}).get(str(year), {}).get("decree", "не указан")
        warnings.append(
            data["no_warning_template"].format(year=year, decree=decree)
        )
        if data.get("transfers", {}).get(str(year), {}).get("amendments_found") is False:
            warnings.append(
                "Проверено: изменяющих актов к постановлению о переносах не найдено "
                "(скан официального портала)."
            )
    else:
        warnings.append(data["warning_template"].format(year=year))
    if region is None:
        warnings.append(REGIONAL_NOTE + " (укажите --region <код> при необходимости)")
    else:
        code = normalize_region(region)
        warnings.append(f"Учтены региональные нерабочие праздничные дни: {region_name(code)} (код {code}).")
    return warnings


def unknown_years(years, calendar_path: Optional[str] = None) -> Set[int]:
    """Years in the span that have no declared holiday set at all."""
    index = _holiday_index(calendar_path)
    return {y for y in years if y not in index}


def calendar_check() -> dict:
    """Validate the isolated transfer manifest and report its coverage."""
    try:
        data = json.loads(TRANSFERS_PATH.read_text(encoding="utf-8"))
        through = int(data["verified_through"])
        years = sorted(int(year) for year in data["years"])
        if years != list(range(min(years), through + 1)):
            raise CalendarError("government transfer years are not contiguous")
        return {"status": "ok", "valid": True, "verified_through": through, "years": years, "source": data["source"]}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, CalendarError) as exc:
        return {"status": "error", "valid": False, "detail": str(exc)}


def add_working_days(start: date, count: int, calendar_path: Optional[str] = None, region=None) -> date:
    """Advance ``count`` working days per Б1 (ч. 3 + ч. 4 ст. 113 АПК РФ).

    ``start`` is Day 1 — the next calendar day after the event — and it counts
    as day 1 when it is a working day. Non-working days are skipped without
    incrementing the counter. So a Friday event (start = Saturday) skips
    Saturday and Sunday and returns the following Friday for ``count=3``,
    while a Thursday event (start = Friday) counts that Friday as day 1.

    Getting this off by one silently shifts every АПК deadline by a day, so the
    distinction is asserted in tests on both a mid-week and a weekend start.
    """
    if count <= 0:
        return start
    current = start
    counted = 0
    while counted < count:
        if is_working_day(current, calendar_path, region):
            counted += 1
            if counted == count:
                break
        current += timedelta(days=1)
    return current


def add_calendar_days(start: date, count: int) -> date:
    """Advance ``count`` calendar days (ГПК/КАС)."""
    return start + timedelta(days=count - 1)
