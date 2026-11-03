# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S1-13: historical_court_tariffs больше не мёртвые данные — тариф по дате
подачи (контракт S-41 «дата-тариф»)."""
from __future__ import annotations

from sud_cli.legal_engine import (
    STATUS_MANUAL_REVIEW,
    STATUS_OK,
    calculate_fee,
    historical_edition_for,
    load_rules,
)


def test_edition_selected_for_pre_2024_date():
    rules = load_rules()
    ed = historical_edition_for("2020-06-01", rules)
    assert ed is not None and ed["edition_id"] == "nk_rf_221_fz_2015"


def test_no_edition_for_current_dates():
    rules = load_rules()
    assert historical_edition_for("2025-01-01", rules) is None
    assert historical_edition_for("2024-09-09", rules) is None


def test_property_claim_differs_between_editions():
    rules = load_rules()
    old = calculate_fee(claim_amount=10_000_000, jurisdiction="arbitration",
                        fee_kind="property_claim", rules=rules, as_of="2020-01-15")
    new = calculate_fee(claim_amount=10_000_000, jurisdiction="arbitration",
                        fee_kind="property_claim", rules=rules, as_of="2025-01-15")
    assert old.status == STATUS_OK and new.status == STATUS_OK
    assert old.amount != new.amount, "тарифы редакций не различаются"
    assert "221-ФЗ" in old.formula
    assert "221-ФЗ" not in new.formula


def test_kind_missing_in_old_edition_is_manual_review():
    rules = load_rules()
    res = calculate_fee(claim_amount=100_000, jurisdiction="arbitration",
                        fee_kind="bankruptcy_creditor_claim", rules=rules,
                        as_of="2020-01-15")
    assert res.status == STATUS_MANUAL_REVIEW
    assert "221-ФЗ" in res.detail


def test_kas_falls_back_to_current_tables():
    # В исторической редакции нет КАС — jurisdiction unknown, не подмена.
    rules = load_rules()
    res = calculate_fee(claim_amount=100_000, jurisdiction="kas",
                        fee_kind="property_claim", rules=rules, as_of="2020-01-15")
    assert res.status == STATUS_MANUAL_REVIEW
