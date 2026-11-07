# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S1-14: календарь — зашитые переносы 2024/2025, валидация шаблонов, регион-алиас."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from sud_cli.production_calendar import (
    CalendarError,
    holidays_for_year,
    is_working_day,
    load_calendar,
)
from sud_cli.regional_holidays import normalize_region

CAL = Path(__file__).resolve().parent.parent / "sud_cli" / "holidays_rf.json"


# --- переносы 2024 (ПП от 10.08.2023 № 1314) ---

def test_2024_transfer_days_off():
    for d in (date(2024, 5, 10), date(2024, 12, 31), date(2024, 4, 29),
              date(2024, 4, 30), date(2024, 12, 30)):
        assert not is_working_day(d), d


def test_2024_working_saturdays():
    for d in (date(2024, 4, 27), date(2024, 11, 2), date(2024, 12, 28)):
        assert is_working_day(d), d


def test_2024_yearly_count_matches_official():
    working = sum(1 for m in range(1, 13) for day in range(1, 32)
                  if _safe_working(2024, m, day))
    assert working == 248  # официальный производственный календарь 2024


# --- переносы 2025 (ПП от 04.10.2024 № 1335) ---

def test_2025_transfer_days_off():
    for d in (date(2025, 5, 2), date(2025, 12, 31), date(2025, 5, 8),
              date(2025, 6, 13), date(2025, 11, 3)):
        assert not is_working_day(d), d


def test_2025_autotransfer_overridden_by_decree():
    # Без записей auto_transfers_st112 код автоматически сделал бы
    # 24.02 и 10.03 выходными; постановление переносит эти выходные на
    # 08.05 и 13.06, поэтому оба понедельника рабочие.
    assert is_working_day(date(2025, 2, 24))
    assert is_working_day(date(2025, 3, 10))


def test_2025_nov_1_working_saturday():
    # Двусторонний перенос: 01.11.2025 (сб) становится рабочим, 03.11 — выходным.
    assert is_working_day(date(2025, 11, 1))
    assert not is_working_day(date(2025, 11, 3))


def test_2025_holiday_sources_stay_off():
    # Праздничные исходные даты переносов остаются нерабочими.
    for d in (date(2025, 1, 4), date(2025, 1, 5), date(2025, 2, 23),
              date(2025, 3, 8), date(2025, 11, 4)):
        assert not is_working_day(d), d


def test_2025_yearly_count_matches_official():
    working = sum(1 for m in range(1, 13) for day in range(1, 32)
                  if _safe_working(2025, m, day))
    assert working == 247  # официальный производственный календарь 2025


# --- валидация шаблонов warning ---

def test_broken_warning_template_rejected_at_load(tmp_path):
    data = json.loads(CAL.read_text(encoding="utf-8"))
    data["warning_template"] = "битый {unknown_placeholder}"
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CalendarError):
        load_calendar(str(p))


def test_valid_calendar_loads():
    assert load_calendar()["transfer_decrees_encoded"] is True


# --- мёртвый регион-алиас ---

def test_region_alias_9_removed():
    with pytest.raises(ValueError):
        normalize_region("9")


def _safe_working(year, month, day):
    from calendar import monthrange
    if day > monthrange(year, month)[1]:
        return False
    return is_working_day(date(year, month, day))
