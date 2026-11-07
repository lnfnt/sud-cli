# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""W6/§2 (S1-3): модель приостановления исковой давности.

Заключение colleague 2026-11-01: п. 3 ст. 202 ГК РФ + п. 16 ППВС № 43 —
порядок приостанавливает срок (итог = конец порядка + остаток на начало);
п. 4 ст. 202 ГК РФ — остаток <6 мес. или порядок за пределами срока →
продление до 6 месяцев с конца порядка.
"""
from __future__ import annotations

from datetime import date

from sud_cli.limitation import calculate_limitation


def test_general_case_suspension():
    # Срок 3 года с 01.01.2020 → базовое окончание 01.01.2023.
    # Порядок начат 01.01.2021 (прошло 1 год, остаток 24 мес.), 30 дней →
    # конец 31.01.2021. Итог: 31.01.2021 + 24 мес. = 31.01.2023.
    r = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                             pretrial_start=date(2021, 1, 1),
                             pretrial_end=date(2021, 1, 31),
                             check_date=date(2022, 6, 1))
    assert r.status == "ok"
    assert not r.is_expired
    assert r.expiry_date == "2023-01-31", r.expiry_date
    assert "п. 3 ст. 202 ГК РФ" in r.legal_basis[0]
    assert "№ 43" in r.legal_basis[0]


def test_less_than_six_months_remainder_extends_to_six_months():
    # Порядок начат за 4 мес. до конца срока → продление до 6 мес. с конца.
    r = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                             pretrial_start=date(2022, 9, 1),
                             pretrial_end=date(2022, 9, 30),
                             check_date=date(2023, 1, 1))
    assert r.status == "ok"
    assert r.expiry_date == "2023-03-30", r.expiry_date
    assert "п. 4 ст. 202 ГК РФ" in r.legal_basis[0]


def test_pretrial_started_after_expiry_extends_to_six_months():
    # Порядок начат уже за пределами срока → п. 4: 6 мес. с конца порядка.
    r = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                             pretrial_start=date(2024, 6, 1),
                             pretrial_end=date(2024, 7, 1),
                             check_date=date(2024, 8, 1))
    assert r.status == "ok"
    # 01.07.2024 + 6 мес. = 01.01.2025 — праздник, ст. 193 ГК переносит
    # на первый рабочий день (09.01.2025). Перенос — часть модели.
    assert r.expiry_date == "2025-01-09", r.expiry_date
    assert "п. 4 ст. 202 ГК РФ" in r.legal_basis[0]


def test_pretrial_start_before_limitation_start_is_invalid():
    r = calculate_limitation(start=date(2022, 1, 1), period_type="general",
                             pretrial_start=date(2021, 1, 1),
                             pretrial_end=date(2021, 2, 1))
    assert r.status == "error"
    assert "INVALID_ARGUMENTS" in r.detail


def test_pretrial_dates_must_come_in_pair():
    r = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                             pretrial_start=date(2022, 1, 1))
    assert r.status == "error"
    assert "INVALID_ARGUMENTS" in r.detail


def test_legacy_claim_notice_days_still_works():
    # Обратная совместимость старых вызовов без дат.
    r = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                             claim_notice_days=30, check_date=date(2024, 2, 1))
    assert r.status == "ok"
    assert r.is_expired
    assert "ст. 202 ГК РФ" in r.legal_basis[0]


def test_trace_describes_suspension_steps():
    r = calculate_limitation(start=date(2020, 1, 1), period_type="general",
                             pretrial_start=date(2022, 1, 1),
                             pretrial_end=date(2022, 1, 31))
    steps = " | ".join(s["step"] for s in r.trace)
    assert "остаток" in steps and "2022-01-31" in steps and "итоговая дата" in steps
