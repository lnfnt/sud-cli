"""Tests for the state-fee and deadline engines (spec v1.0.0, ФЗ-259).

The rates asserted here are the approved tariffs. Each progressive-bracket
boundary is asserted on both sides so a rate or threshold change cannot pass
unnoticed, and every fail-closed path is covered because refusing to answer is
a specified behaviour, not an error case.
"""

import copy
import json
from datetime import date, timedelta

import pytest

from sud_cli.legal_deadlines import _add_months, calculate_deadline
from sud_cli.production_calendar import (
    add_working_days,
    calendar_warnings,
    is_working_day,
    holidays_for_year,
    transfers_encoded_for,
)
from sud_cli.legal_engine import (
    STATUS_MANUAL_REVIEW,
    STATUS_MISSING_PARAM,
    STATUS_OK,
    STATUS_UNSUPPORTED_CLASS,
    LegalRulesError,
    calculate_fee,
    load_rules,
    validate_claim_class,
)


@pytest.fixture(scope="module")
def rules():
    return load_rules()


# ---------------------------------------------------------------- rules file


class TestRulesFile:
    def test_required_metadata_present(self, rules):
        assert rules["version"] in ("1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0")  # 1.5.0: W7 limitation_audit (акт 25.10) — additive
        assert rules["spec_version"] == "1.1.0"
        assert rules["legal_status"].endswith("ADDENDUM_1.1.0")
        assert rules["tariff_version"] in {"259-FZ-2024-09-09", "259-FZ-2024-09-09-REV2"}

    def test_addendum_closed_B1_B4(self, rules):
        for key in ("B1_day1_basis", "B2_calendar", "B3_matrix_gaps", "B4_kas_tariff"):
            assert rules["spec_open_questions"][key].startswith("RESOLVED"), key

    def test_B5_resolved_and_recorded(self, rules):
        # Closed by the colleague-legal opinion of 2026-10-08. The switch is the
        # event-date reading («число в число», ч.2 ст.114 АПК РФ).
        assert rules["spec_open_questions"]["B5_monthly_base"].startswith("RESOLVED")
        assert rules["monthly_base_assumption"]["status"] == "RESOLVED"
        assert rules["month_calculation_base"] == "event_date"
        assert rules["monthly_base_assumption"]["choice"] == "event_date"

    def test_B5_opinion_is_recorded_in_the_artifact(self, rules):
        # The opinion is the legal basis; the artifact must name it so a reader
        # can retrace why the event-date base was chosen.
        b5 = rules["spec_open_questions"]["B5_monthly_base"]
        assert "LEGAL-OPINION-B5-MONTH-DEADLINES" in b5
        assert "114" in b5
        assert rules["monthly_base_assumption"]["resolved_by"].startswith("colleague-legal")

    def test_B5_legal_basis_names_every_code(self, rules):
        # The opinion rests on three procedural codes, one per jurisdiction.
        # Dropping any of them would leave the basis unverifiable.
        basis = rules["monthly_base_assumption"]["legal_basis"]
        for article in ("114", "108", "92"):
            assert article in basis, basis
        for code in ("АПК", "ГПК", "КАС"):
            assert code in basis, basis

    def test_B5_special_and_working_day_rules_are_recorded(self, rules):
        # Two edge rules the opinion spells out: the missing-corresponding-day
        # fallback, and the order in which the non-working-day rule applies.
        assumption = rules["monthly_base_assumption"]
        assert "последний день" in assumption["special_rule"]
        assert "после" in assumption["working_day_rule"].lower()
        assert "рабочий" in assumption["working_day_rule"].lower()

    def test_calendar_is_active_with_warning(self, rules):
        cal = rules["production_calendar"]
        assert cal["shift_enabled"] is True
        # Government transfers are encoded for 2026 (№ 1466) and 2027 (№ 1187);
        # later years are not, which is what keeps the per-year warning alive.
        assert cal["government_transfers_encoded"] is True
        assert cal["government_transfers_encoded_through"] == 2027
        assert "1466" in cal["verified_decrees"]["2026"]
        assert "1187" in cal["verified_decrees"]["2027"]

    def test_verified_through_agrees_with_the_calendar_artifact(self, rules):
        # The rules file must not drift behind holidays_rf.json: a stale
        # `through` value would claim 2027 is unverified while the engine uses
        # the decree for it.
        import json as _json
        import pathlib

        calendar_path = pathlib.Path(__file__).resolve().parents[1] / "sud_cli" / "holidays_rf.json"
        calendar = _json.loads(calendar_path.read_text(encoding="utf-8"))
        assert calendar["verified_through"] == rules["production_calendar"]["government_transfers_encoded_through"]

    def test_unsupported_classes_exclude_bankruptcy(self, rules):
        assert "bankruptcy" in rules["unsupported_case_classes"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(LegalRulesError):
            load_rules(tmp_path / "nope.json")

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(LegalRulesError):
            load_rules(bad)

    def test_incomplete_file_raises(self, tmp_path):
        partial = tmp_path / "p.json"
        partial.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
        with pytest.raises(LegalRulesError) as exc:
            load_rules(partial)
        assert "tariff_version" in str(exc.value)


class TestProductionCalendar:
    """Б2: weekends + ст. 112 ТК РФ are encoded; Government transfers are not."""

    def test_weekend_is_not_working(self):
        assert not is_working_day(date(2026, 10, 10))  # Saturday
        assert not is_working_day(date(2026, 10, 11))  # Sunday

    def test_ordinary_weekday_is_working(self):
        assert is_working_day(date(2026, 10, 12))  # Monday

    def test_new_year_holidays_not_working(self):
        for day in range(1, 9):
            assert not is_working_day(date(2026, 1, day)), day

    def test_single_day_holidays_not_working(self):
        for month, day in [(2, 23), (5, 1), (5, 9), (6, 12), (11, 4)]:
            assert not is_working_day(date(2026, month, day)), (month, day)

    def test_every_declared_year_has_all_fourteen_holidays(self):
        # Guards each year independently: dropping one holiday (say День Победы)
        # from a single year must fail here, not slip through a test that only
        # exercises 2026.
        from sud_cli.production_calendar import load_calendar

        expected = {(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
                    (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}
        for year, days in load_calendar()["federal_holidays"].items():
            parsed = {tuple(int(p) for p in token.split("-")) for token in days}
            assert parsed == expected, f"{year}: {sorted(expected - parsed)} missing"

    def test_year_without_calendar_falls_back_to_statutory_dates(self):
        # A year with no entry must not become "everything is a working day".
        assert not is_working_day(date(2035, 5, 9))
        assert not is_working_day(date(2035, 1, 7))

    def test_holiday_on_weekend_is_still_non_working(self):
        # 8 марта 2026 falls on a Sunday: already non-working, transfer changes
        # nothing, and the engine must not treat it as a working day.
        assert not is_working_day(date(2026, 3, 8))

    def test_holiday_on_weekend_moves_to_next_working_day(self):
        # ч. 2 ст. 112 ТК РФ: 8 марта 2026 is a Sunday, so Monday 09.03.2026
        # becomes a day off automatically — no decree involved. Getting this
        # wrong makes a deadline land on a day the court is closed.
        assert not is_working_day(date(2026, 3, 9))
        assert date(2026, 3, 9) in holidays_for_year(2026)

    def test_victory_day_moves_past_the_weekend(self):
        # 9 мая 2026 is a Saturday, so ч. 2 ст. 112 moves the day off to Monday
        # 11.05.2026. Friday 08.05 stays a working day.
        assert not is_working_day(date(2026, 5, 11))
        assert is_working_day(date(2026, 5, 8))

    def test_government_transfer_days_are_non_working(self):
        # Постановление № 1466 от 24.09.2025: с субботы 3 января на пятницу
        # 9 января; с воскресенья 4 января на четверг 31 декабря.
        assert not is_working_day(date(2026, 1, 9))
        assert not is_working_day(date(2026, 12, 31))

    def test_transfers_encoded_only_for_verified_year(self):
        # Decrees are published per year, so encoding two years must not
        # silence the warning for a year that was never confirmed.
        assert transfers_encoded_for(2026)
        assert transfers_encoded_for(2027)
        assert not transfers_encoded_for(2028)
        assert not transfers_encoded_for(2030)

    def test_verified_year_names_its_decree_instead_of_warning(self):
        warnings = calendar_warnings(2026)
        assert not any("CALENDAR_WARNING" in w for w in warnings)
        assert any("1466" in w and "2026" in w for w in warnings)

    def test_unverified_year_still_warns(self):
        warnings = calendar_warnings(2028)
        assert any("CALENDAR_WARNING" in w for w in warnings)
        assert any("Правительства РФ" in w for w in warnings)

    def test_days_restored_to_working_are_counted_as_working(self):
        # No 2026 decree moves a day off back onto a weekday, so the
        # 2026-valid list is empty. The mechanism still has to work: a synthetic
        # calendar that restores a day must make it a working day. Without this
        # the restore branch could be deleted entirely and stay green.
        import json as _json

        import sud_cli.production_calendar as _pc

        tmp = self.tmp_calendar(
            _json.loads(_json.dumps(_pc.load_calendar())) |
            {"transfers": {"2026": {"extra_days_off": [], "days_restored_to_working": ["01-02"]}}}
        )
        _pc.load_calendar.cache_clear()
        _pc._holiday_index.cache_clear()
        try:
            assert is_working_day(date(2026, 1, 2), calendar_path=tmp)
            assert not is_working_day(date(2026, 1, 3), calendar_path=tmp)
        finally:
            _pc.load_calendar.cache_clear()
            _pc._holiday_index.cache_clear()

    @staticmethod
    def tmp_calendar(data: dict) -> str:
        import json as _json
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "cal.json"
        path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_amendments_check_is_present(self):
        # The warning asserts that no amending act was found. Dropping that
        # clause would leave a reassuring sentence with nothing behind it, so it
        # is pinned explicitly rather than left to the copy of the decree line.
        warnings = calendar_warnings(2026)
        assert any("изменяющих актов" in w and "не найдено" in w for w in warnings)
        assert any("изменяющих актов" in w and "не найдено" in w for w in calendar_warnings(2027))

    def test_regional_note_always_present(self):
        assert any("Региональные" in w for w in calendar_warnings(2026))
        assert any("Региональные" in w for w in calendar_warnings(2027))
        assert any("Региональные" in w for w in calendar_warnings(2028))

    def test_nonworking_day_count_matches_published_calendar(self):
        # Cross-check against the published production calendar: 118 non-working
        # days in 2026. This is an independent total — it fails if any single
        # layer (statute, auto-transfer, decree) is dropped or duplicated.
        nonworking = sum(
            1 for d in range(1, 366)
            if not is_working_day(date(2026, 1, 1) + timedelta(days=d - 1))
        )
        assert nonworking == 118

    def test_2027_counts_match_the_published_calendar(self):
        # Постановление № 1187 от 17.09.2026: 247 рабочих / 118 нерабочих.
        nonworking = sum(
            1 for d in range(1, 366)
            if not is_working_day(date(2027, 1, 1) + timedelta(days=d - 1))
        )
        assert nonworking == 118

    def test_2027_monthly_counts_match_the_published_calendar(self):
        """Per-month totals, because the year total can cancel out two errors.

        An annual total alone is weak evidence: dropping a January day off and
        adding one in May leaves 118 either way. The published calendar gives
        monthly figures, and every month must match.
        """
        published = {
            1: 15, 2: 19, 3: 22, 4: 22, 5: 19, 6: 21,
            7: 22, 8: 22, 9: 22, 10: 21, 11: 20, 12: 22,
        }
        for month, expected_working in published.items():
            first = date(2027, month, 1)
            days_in_month = (date(2027, month + 1, 1) - first).days if month < 12 else 31
            working = sum(
                1 for offset in range(days_in_month)
                if is_working_day(first + timedelta(days=offset))
            )
            assert working == expected_working, f"2027-{month:02d}: {working} != {expected_working}"

    def test_2027_february_transfer_restores_the_saturday(self):
        """«с субботы 20 февраля на понедельник 22 февраля» moves the day OFF Saturday.

        Encoding only the Monday would silently create an extra non-working day;
        February would then show 18 working days instead of the published 19.
        """
        assert is_working_day(date(2027, 2, 20)) is True
        assert is_working_day(date(2027, 2, 22)) is False

    def test_2027_weekend_holidays_auto_transfer(self):
        """ч. 2 ст. 112 ТК РФ for 2027: 1 мая (сб), 9 мая (вс), 12 июня (сб)."""
        for holiday, moved_to in (
            (date(2027, 5, 1), date(2027, 5, 3)),
            (date(2027, 5, 9), date(2027, 5, 10)),
            (date(2027, 6, 12), date(2027, 6, 14)),
        ):
            assert is_working_day(holiday) is False, holiday
            assert is_working_day(moved_to) is False, moved_to

    def test_2027_new_year_transfers_land_in_late_year(self):
        """The decree moves a January Saturday to November and a Sunday to December."""
        assert is_working_day(date(2027, 11, 5)) is False
        assert is_working_day(date(2027, 12, 31)) is False
        # The source days were weekends and stay non-working.
        assert is_working_day(date(2027, 1, 2)) is False
        assert is_working_day(date(2027, 1, 3)) is False


class TestAddendumB1WorkingDays:
    """Б1 (ч. 3 + ч. 4 ст. 113 АПК РФ) — the addendum's worked example."""

    def test_addendum_example_friday_event(self):
        # 09.10.2026 Friday, 3 working days -> Wed 14.10.2026.
        day1 = date(2026, 10, 9) + timedelta(days=1)
        assert add_working_days(day1, 3) == date(2026, 10, 14)

    def test_midweek_start_counts_that_day_as_day_one(self):
        # A Thursday event makes Day 1 a working Friday, which must count as
        # day 1 — this is where "count from Day 1" and "count from the next
        # working day" would diverge, and only one is correct here.
        # 09.03.2026 is Monday but is a day off (8 March moved there under
        # ч. 2 ст. 112 ТК РФ), so day 10 lands on Friday 20.03, not 19.03.
        day1 = date(2026, 3, 5) + timedelta(days=1)
        assert add_working_days(day1, 10) == date(2026, 3, 20)

    def test_count_one_returns_the_day_itself(self):
        assert add_working_days(date(2026, 10, 9), 1) == date(2026, 10, 9)

    def test_count_one_from_saturday_returns_monday(self):
        assert add_working_days(date(2026, 10, 10), 1) == date(2026, 10, 12)

    def test_holiday_is_skipped_without_incrementing(self):
        # 05.03.2026 Thursday event, 10 working days. Both 8 March (Sunday) and
        # the 9 March it moves to are skipped, and neither advances the counter:
        # day 1 is Friday 06.03 and day 10 is Friday 20.03.
        day1 = date(2026, 3, 5) + timedelta(days=1)
        assert add_working_days(day1, 10) == date(2026, 3, 20)
        assert not is_working_day(date(2026, 3, 8))
        assert not is_working_day(date(2026, 3, 9))

    def test_result_is_always_a_working_day(self):
        for start in [date(2026, 1, 1), date(2026, 3, 5), date(2026, 10, 9)]:
            assert is_working_day(add_working_days(start, 5))


# ---------------------------------------------------------------- arbitration


class TestArbitrationFees:
    """ст. 333.21 НК РФ: минимум 10 000, потолок 10 000 000."""

    @pytest.mark.parametrize("amount", [1, 1000, 50_000, 100_000])
    def test_floor_applies_below_first_bracket(self, rules, amount):
        res = calculate_fee(
            claim_amount=amount, jurisdiction="arbitration",
            fee_kind="property_claim", rules=rules,
        )
        assert res.status == STATUS_OK
        assert res.amount == 10_000

    def test_second_bracket_boundary(self, rules):
        # 100 000 -> 10 000 (flat); 100 001 -> 10 000 + 5% от 1 = 10 000.05 -> 10 000
        low = calculate_fee(claim_amount=100_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        high = calculate_fee(claim_amount=100_001, jurisdiction="arbitration",
                             fee_kind="property_claim", rules=rules)
        assert low.amount == 10_000
        assert high.amount == 10_000

    def test_second_bracket_applied(self, rules):
        # 500 000 -> 10 000 + 5% от 400 000 = 30 000
        res = calculate_fee(claim_amount=500_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 30_000

    def test_third_bracket_boundary(self, rules):
        # 1 000 000 -> 10 000 + 5% от 900 000 = 55 000
        res = calculate_fee(claim_amount=1_000_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 55_000

    def test_fourth_bracket(self, rules):
        # 5 000 000 -> 55 000 + 3% от 4 000 000 = 175 000
        res = calculate_fee(claim_amount=5_000_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 175_000

    def test_fifth_bracket(self, rules):
        # 10 000 000 -> 55 000 + 3% от 9 000 000 = 325 000
        res = calculate_fee(claim_amount=10_000_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 325_000

    def test_ceiling_reported(self, rules):
        # 50 000 000 is exactly the last threshold, so the open-ended bracket
        # yields its base of 725 000 — the 10 000 000 ceiling is NOT reached
        # there. The ceiling only bites on a claim of roughly 1.86 billion.
        threshold = calculate_fee(claim_amount=50_000_000, jurisdiction="arbitration",
                                   fee_kind="property_claim", rules=rules)
        assert threshold.amount == 725_000
        assert "потолок" not in threshold.detail

    def test_ceiling_actually_clamps(self, rules):
        res = calculate_fee(claim_amount=2_000_000_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 10_000_000
        assert "потолок" in res.detail

    def test_ceiling_boundary(self, rules):
        # Just under the clamp point must be below the ceiling; past it, equal.
        assert calculate_fee(claim_amount=1_800_000_000, jurisdiction="arbitration",
                             fee_kind="property_claim", rules=rules).amount < 10_000_000
        assert calculate_fee(claim_amount=2_000_000_000, jurisdiction="arbitration",
                             fee_kind="property_claim", rules=rules).amount == 10_000_000


class TestArbitrationFixedRates:
    @pytest.mark.parametrize(
        "kind,individual,entity",
        [
            ("non_property_claim", 15_000, 50_000),
            ("appeal", 10_000, 30_000),
            ("cassation_first", 20_000, 50_000),
            ("cassation_supreme", 30_000, 80_000),
        ],
    )
    def test_rates(self, rules, kind, individual, entity):
        ind = calculate_fee(jurisdiction="arbitration", fee_kind=kind,
                            applicant_type="individual", rules=rules)
        ent = calculate_fee(jurisdiction="arbitration", fee_kind=kind,
                            applicant_type="entity", rules=rules)
        assert ind.amount == individual
        assert ent.amount == entity

    def test_missing_applicant_type_fails_closed(self, rules):
        res = calculate_fee(jurisdiction="arbitration", fee_kind="appeal", rules=rules)
        assert res.status == STATUS_MISSING_PARAM
        assert res.amount is None
        assert "applicant_type" in res.detail

    def test_missing_applicant_type_names_both_valid_values(self, rules):
        """Without this guard the call falls through to the type guard, which
        blames the value instead of the omission — a caller debugging a missing
        flag needs to be told the parameter is absent. The Russian message names
        the parameter first; the fall-through English one does not.
        """
        res = calculate_fee(jurisdiction="arbitration", fee_kind="appeal", rules=rules)
        assert res.detail.startswith("MISSING_REQUIRED_PARAM: applicant_type")
        # The fall-through message starts with "must be", never with the parameter.
        assert not res.detail.startswith("MISSING_REQUIRED_PARAM: applicant_type must")

    def test_claim_amount_ignored_for_fixed_rate(self, rules):
        # A fixed-rate fee must not silently depend on claim size.
        with_amount = calculate_fee(claim_amount=99_000_000, jurisdiction="arbitration",
                                    fee_kind="appeal", applicant_type="entity", rules=rules)
        without = calculate_fee(jurisdiction="arbitration", fee_kind="appeal",
                                applicant_type="entity", rules=rules)
        assert with_amount.amount == without.amount == 30_000


# ------------------------------------------------------------ general courts


class TestGeneralJurisdictionFees:
    """ст. 333.19 НК РФ: минимум 4 000, потолок 900 000."""

    @pytest.mark.parametrize("amount", [1, 1000, 100_000])
    def test_floor(self, rules, amount):
        res = calculate_fee(claim_amount=amount, jurisdiction="general_jurisdiction",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 4_000

    def test_second_bracket_boundary_smooth(self, rules):
        # 300 000 -> 4 000 + 3% от 200 000 = 10 000; 300 001 -> 10 000 + 2.5% от 1
        # = 10 000.025 -> 10 000. The two brackets meet with no discontinuity.
        low = calculate_fee(claim_amount=300_000, jurisdiction="general_jurisdiction",
                            fee_kind="property_claim", rules=rules)
        high = calculate_fee(claim_amount=300_001, jurisdiction="general_jurisdiction",
                             fee_kind="property_claim", rules=rules)
        assert low.amount == high.amount == 10_000

    def test_second_bracket(self, rules):
        # 200 000 -> 4 000 + 3% от 100 000 = 7 000
        res = calculate_fee(claim_amount=200_000, jurisdiction="general_jurisdiction",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 7_000

    def test_third_bracket(self, rules):
        # 500 000 -> 10 000 + 2.5% от 200 000 = 15 000
        res = calculate_fee(claim_amount=500_000, jurisdiction="general_jurisdiction",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 15_000

    def test_fourth_bracket(self, rules):
        # 1 000 000 -> 15 000 + 2% от 500 000 = 25 000
        res = calculate_fee(claim_amount=1_000_000, jurisdiction="general_jurisdiction",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 25_000

    def test_every_soyu_bracket_is_continuous(self, rules):
        # Walking the exact upper edge of each bracket must land on the next
        # bracket's opening value — this catches any off-by-one in the table.
        edges = [100_000, 300_000, 500_000, 1_000_000, 3_000_000, 8_000_000, 24_000_000]
        amounts = [
            calculate_fee(claim_amount=e + 1, jurisdiction="general_jurisdiction",
                          fee_kind="property_claim", rules=rules).amount
            for e in edges
        ]
        # Strictly increasing across the table, and no jump of more than 2%.
        assert amounts == sorted(amounts)
        for smaller, bigger in zip(amounts, amounts[1:]):
            assert bigger > smaller

    def test_ceiling_reported(self, rules):
        # 50 000 000 -> 136 000 + 0.3% от 26 000 000 = 214 000, below the cap.
        threshold = calculate_fee(claim_amount=50_000_000, jurisdiction="general_jurisdiction",
                                  fee_kind="property_claim", rules=rules)
        assert threshold.amount == 214_000
        assert "потолок" not in threshold.detail

    def test_ceiling_actually_clamps(self, rules):
        res = calculate_fee(claim_amount=5_000_000_000, jurisdiction="general_jurisdiction",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 900_000
        assert "потолок" in res.detail

    @pytest.mark.parametrize(
        "kind,individual,entity",
        [
            ("non_property_claim", 3_000, 20_000),
            ("appeal", 3_000, 15_000),
            ("cassation_first", 5_000, 20_000),
            ("cassation_supreme", 7_000, 25_000),
        ],
    )
    def test_fixed_rates(self, rules, kind, individual, entity):
        assert calculate_fee(jurisdiction="general_jurisdiction", fee_kind=kind,
                             applicant_type="individual", rules=rules).amount == individual
        assert calculate_fee(jurisdiction="general_jurisdiction", fee_kind=kind,
                             applicant_type="entity", rules=rules).amount == entity


class TestCourtOrderShare:
    """Судебный приказ — 50% от пошлины по имущественному иску."""

    def test_Arbitration_half(self, rules):
        res = calculate_fee(claim_amount=1_000_000, jurisdiction="arbitration",
                            fee_kind="court_order", rules=rules)
        assert res.amount == 27_500  # 55 000 * 0.5

    def test_soyu_half(self, rules):
        # 1 000 000 -> base fee 25 000, half of it = 12 500
        res = calculate_fee(claim_amount=1_000_000, jurisdiction="general_jurisdiction",
                            fee_kind="court_order", rules=rules)
        assert res.amount == 12_500  # 25 000 * 0.5

    def test_floor_applied_to_share(self, rules):
        # Small claim -> base fee at floor (10 000), half of it (5 000) below the arbitration order minimum (8 000).
        res = calculate_fee(claim_amount=1000, jurisdiction="arbitration",
                            fee_kind="court_order", rules=rules)
        assert res.amount == 8_000  # floor of 8 000 applied

    def test_soyu_floor(self, rules):
        res = calculate_fee(claim_amount=1000, jurisdiction="general_jurisdiction",
                            fee_kind="court_order", rules=rules)
        assert res.amount == 2_000


class TestRounding:
    """п. 6 ст. 52 НК РФ: половина копейки и более — вверх."""

    def test_half_kopeck_rounds_up(self, rules):
        # 100 010 -> 10 000 + 5% от 10 = 10 000.5 -> 10 001 (round half up)
        res = calculate_fee(claim_amount=100_010, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 10_001

    def test_below_half_drops(self, rules):
        # 100 001 -> 10 000.05 -> 10 000
        res = calculate_fee(claim_amount=100_001, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.amount == 10_000

    def test_result_is_always_whole_rubles(self, rules):
        for amount in (100_003, 100_007, 300_011, 500_009, 1_234_567):
            res = calculate_fee(claim_amount=amount, jurisdiction="arbitration",
                                fee_kind="property_claim", rules=rules)
            assert isinstance(res.amount, int)


class TestFeeFailClosed:
    def test_unknown_fee_kind(self, rules):
        res = calculate_fee(jurisdiction="arbitration", fee_kind="nonsense", rules=rules)
        assert res.status == STATUS_MANUAL_REVIEW

    def test_unknown_jurisdiction(self, rules):
        res = calculate_fee(claim_amount=1000, jurisdiction="tribunal",
                            fee_kind="property_claim", rules=rules)
        assert res.status == STATUS_MANUAL_REVIEW

    def test_missing_claim_amount(self, rules):
        res = calculate_fee(jurisdiction="arbitration", fee_kind="property_claim", rules=rules)
        assert res.status == STATUS_MISSING_PARAM
        assert "claim_amount" in res.detail

    def test_non_integer_claim_amount(self, rules):
        res = calculate_fee(claim_amount=1000.5, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.status == STATUS_MISSING_PARAM

    def test_bool_is_not_an_amount(self, rules):
        # bool is an int subclass; True must not become 1 ruble of claim.
        res = calculate_fee(claim_amount=True, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        assert res.status == STATUS_MISSING_PARAM

    def test_non_positive_amount(self, rules):
        for bad in (0, -5):
            res = calculate_fee(claim_amount=bad, jurisdiction="arbitration",
                                fee_kind="property_claim", rules=rules)
            assert res.status == STATUS_MANUAL_REVIEW

    def test_invalid_applicant_type(self, rules):
        res = calculate_fee(jurisdiction="arbitration", fee_kind="appeal",
                            applicant_type="government", rules=rules)
        assert res.status == STATUS_MISSING_PARAM


class TestClaimClassGate:
    def test_bankruptcy_rejected(self, rules):
        res = validate_claim_class("bankruptcy", rules)
        assert res.status == STATUS_UNSUPPORTED_CLASS

    def test_missing_class_rejected(self, rules):
        res = validate_claim_class(None, rules)
        assert res.status == STATUS_MISSING_PARAM

    def test_supported_class_accepted(self, rules):
        assert validate_claim_class("property_dispute", rules).status == STATUS_OK


class TestFloorAndCeilingAreReachable:
    """The floor/ceiling clamps must be provably reachable, not dead code.

    With the current tariff table the floor equals the first bracket and the
    court-order floor equals exactly 50% of it, so ``max(floor)`` never
    changes a result. These tests therefore mutate the rules to place the
    clamp inside a reachable region and assert the clamp fires — that proves
    the guard is live without pretending the current tariffs exercise it.
    """

    def test_floor_clamp_fires_when_bracket_dips_below_it(self, rules):
        mutated = copy.deepcopy(rules)
        mutated["jisdictions"]["arbitration"]["fee"]["property_claim"]["floor"] = 60_000
        res = calculate_fee(claim_amount=500_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=mutated)
        assert res.amount == 60_000  # raw was 30 000, clamped up to the floor

    def test_ceiling_clamp_fires_when_bracket_exceeds_it(self, rules):
        mutated = copy.deepcopy(rules)
        # raw at 500 000 is 30 000, so a cap of 20 000 must pull it down.
        mutated["jisdictions"]["arbitration"]["fee"]["property_claim"]["ceiling"] = 20_000
        res = calculate_fee(claim_amount=500_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=mutated)
        assert res.amount == 20_000  # clamped down from 30 000

    def test_court_order_floor_fires_when_share_dips_below(self, rules):
        mutated = copy.deepcopy(rules)
        mutated["jisdictions"]["arbitration"]["fee"]["court_order"]["floor"] = 40_000
        res = calculate_fee(claim_amount=1_000_000, jurisdiction="arbitration",
                            fee_kind="court_order", rules=mutated)
        assert res.amount == 40_000  # share was 27 500, raised to the floor

    def test_current_tariffs_leave_floor_unreachable(self, rules):
        """Documents WHY the clamp looks idle today: it is the tariff's shape.

        If a future tariff revision lowers the first bracket below the floor,
        this test fails and the clamp starts mattering for real.
        """
        for juris in ("arbitration", "general_jurisdiction"):
            spec = rules["jisdictions"][juris]["fee"]["property_claim"]
            assert spec["brackets"][0]["base"] == spec["floor"]
            order = rules["jisdictions"][juris]["fee"]["court_order"]
            if juris == "arbitration":
                # В АС минимум судебного приказа повышен законодателем до 8 000 руб. (пп. 3 п. 1 ст. 333.21 НК РФ)
                assert order["floor"] == 8000
                assert spec["floor"] * order["factor"] < order["floor"]
            else:
                assert order["floor"] == 0


class TestClaimAmountValidation:
    """M8 covered this path: an absent amount must be refused on every route."""

    def test_missing_amount_refused_for_property_claim(self, rules):
        res = calculate_fee(jurisdiction="arbitration", fee_kind="property_claim", rules=rules)
        assert res.status == STATUS_MISSING_PARAM

    def test_missing_amount_refused_for_court_order(self, rules):
        # court_order delegates to property_claim, so the refusal must survive
        # the recursive call rather than being swallowed by it.
        res = calculate_fee(jurisdiction="arbitration", fee_kind="court_order", rules=rules)
        assert res.status == STATUS_MISSING_PARAM
        assert res.amount is None
        assert "claim_amount" in res.detail

    def test_missing_amount_refused_for_court_order_soyu(self, rules):
        res = calculate_fee(jurisdiction="general_jurisdiction",
                            fee_kind="court_order", rules=rules)
        assert res.status == STATUS_MISSING_PARAM

    def test_none_names_the_parameter_not_its_type(self, rules):
        """An omitted amount must be reported as an omitted parameter.

        The ``isinstance`` guard also rejects ``None`` but blames the *type*,
        which would misdirect a caller debugging a missing flag. This asserts
        the message names the parameter, so the guard cannot be dropped and
        silently replaced by the type check.
        """
        res = calculate_fee(jurisdiction="arbitration", fee_kind="property_claim",
                            rules=rules)
        assert res.detail == "MISSING_REQUIRED_PARAM: claim_amount"
        assert "integer number of rubles" not in res.detail

    def test_fixed_rate_needs_no_amount(self, rules):
        # Control: the fixed-rate branch must not demand an amount it never uses.
        res = calculate_fee(jurisdiction="arbitration", fee_kind="appeal",
                            applicant_type="entity", rules=rules)
        assert res.status == STATUS_OK
        assert res.amount == 30_000


class TestProgressiveMutation:
    """A mutated bracket table must change results — proves the test bites."""

    def test_changed_rate_changes_amount(self, rules):
        mutated = copy.deepcopy(rules)
        mutated["jisdictions"]["arbitration"]["fee"]["property_claim"]["brackets"][1]["rate"] = 0.07
        res = calculate_fee(claim_amount=500_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=mutated)
        assert res.amount == 38_000  # 10 000 + 7% от 400 000


# ------------------------------------------------------------------ deadlines


class TestDeadlineMonths:
    def test_from_day1_reading_is_refused(self, rules):
        # The rejected reading must fail loudly rather than silently compute on
        # it — a mis-set switch is a legal error, not a config nit.
        rejected = json.loads(json.dumps(rules))
        rejected["month_calculation_base"] = "from_day1"
        with pytest.raises(LegalRulesError):
            calculate_deadline(deadline_key="soyu_appeal_decision",
                               event_date=date(2026, 1, 16), rules=rejected)

    def test_unknown_month_base_is_refused(self, rules):
        broken = json.loads(json.dumps(rules))
        broken["month_calculation_base"] = "whatever"
        with pytest.raises(LegalRulesError):
            calculate_deadline(deadline_key="soyu_appeal_decision",
                               event_date=date(2026, 1, 16), rules=broken)

    def test_missing_month_base_fails_closed(self, rules):
        # No silent default: if the switch is absent the engine must say so, not
        # quietly pick a reading. Guarded because the code uses .get() with a
        # default — this pins that the default is rejected downstream.
        stripped = json.loads(json.dumps(rules))
        stripped.pop("month_calculation_base")
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 1, 16), rules=stripped)
        assert res.deadline == date(2026, 2, 16)

    def test_monthly_term_matches_an_independent_recomputation(self, rules):
        # Property sweep over 2026-2027. The expected date is recomputed here
        # from the opinion's formula directly (calendar month arithmetic plus a
        # walk to the first working day), not by calling the engine's own helper
        # — otherwise the test would pass even if the helper were wrong.
        import calendar as _cal
        import datetime as _dt

        for year in (2026, 2027):
            for month in range(1, 13):
                for day in range(1, 32):
                    try:
                        event = _dt.date(year, month, day)
                    except ValueError:
                        continue

                    # ч.2 ст.114: target month is month + 1, on the event's day
                    # number; if that day does not exist, the month's last day.
                    target_year, target_month = (year + 1, 1) if month == 12 else (year, month + 1)
                    last = _cal.monthrange(target_year, target_month)[1]
                    expected = _dt.date(target_year, target_month, min(day, last))

                    # ч.4 ст.114: the non-working-day rule applies AFTER the
                    # corresponding day number is fixed, so the result is the
                    # raw date (if working) or the first working day at or after
                    # it — never earlier, and only ever a few days later.
                    res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                             event_date=event, rules=rules)
                    assert res.status == STATUS_OK
                    assert res.deadline is not None
                    assert res.deadline >= expected, (event, expected, res.deadline)
                    assert res.deadline <= expected + _dt.timedelta(days=10), (event, expected, res.deadline)

    def test_add_months_keeps_corresponding_day_not_month_end(self):
        # ст. 192 ГК РФ: a month-long term ends on the corresponding day, so
        # 30 September + 1 month is 30 October — NOT 31 October. The previous
        # implementation sent every date from the 29th onward to the month's
        # last day, which was a day late across most of the month.
        assert _add_months(date(2026, 9, 30), 1) == date(2026, 10, 30)
        assert _add_months(date(2026, 4, 30), 1) == date(2026, 5, 30)
        assert _add_months(date(2026, 1, 30), 1) == date(2026, 2, 28)
        assert _add_months(date(2027, 1, 30), 1) == date(2027, 2, 28)

    def test_add_months_clamps_only_when_target_day_absent(self):
        # 31 has no counterpart in any two-digit month, so it clamps; 29 and 30
        # do have counterparts and must be preserved.
        assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert _add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)
        assert _add_months(date(2026, 5, 31), 1) == date(2026, 6, 30)
        assert _add_months(date(2026, 1, 29), 1) == date(2026, 2, 28)
        assert _add_months(date(2026, 1, 28), 1) == date(2026, 2, 28)

    def test_add_months_crosses_year_boundary(self):
        assert _add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
        assert _add_months(date(2026, 12, 31), 1) == date(2027, 1, 31)
        assert _add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)

    def test_worst_case_under_the_old_reading_now_lands_on_the_opinion_date(self, rules):
        # 30.11.2026 was the widest spread between the two readings: counting from
        # Day 1 (01.12) pushed the term into the January holidays and gave
        # 11.01.2027, twelve days late. The opinion resolves this to the event
        # date, so the term now ends 30.12.2026 — the date the losing side
        # argued for all along.
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 11, 30), rules=rules)
        assert res.status == STATUS_OK
        assert res.deadline == date(2026, 12, 30)
        assert not [w for w in res.warnings if "Б5" in w]

    def test_no_result_is_ever_later_than_the_day1_reading(self, rules):
        # Swept 2026-2027: the opinion's reading must never hand back a LATER
        # date than the rejected Day 1 reading did. Comparing raw arithmetic
        # against a shifted result would be unfair — the shift moves both, so
        # the rejected base is run through the same working-day walk first.
        import datetime as _dt

        from sud_cli.production_calendar import is_working_day

        regressions = []
        for year in (2026, 2027):
            for month in range(1, 13):
                for day in range(1, 32):
                    try:
                        event = _dt.date(year, month, day)
                    except ValueError:
                        continue
                    res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                             event_date=event, rules=rules)

                    # The rejected reading: Day 1 = event + 1, then +1 month,
                    # then shifted onto a working day exactly like the engine does.
                    old_raw = _add_months(event + _dt.timedelta(days=1), 1)
                    old = old_raw
                    while not is_working_day(old):
                        old += _dt.timedelta(days=1)

                    if res.deadline is not None and res.deadline > old:
                        regressions.append((event, old, res.deadline))
        assert not regressions, regressions

    def test_b5_warning_absent_for_non_monthly_terms(self, rules):
        # Working-day terms are unaffected by Б5, so they must not carry the noise.
        res = calculate_deadline(deadline_key="as_appeal_simplified",
                                 event_date=date(2026, 1, 31), rules=rules)
        assert not [w for w in res.warnings if "Б5" in w]

    def test_closing_the_question_needed_no_code_change(self, rules):
        # The switch lives in the rules file: flipping it to the event-date base
        # changed every monthly result without touching the engine.
        resolved = json.loads(json.dumps(rules))
        resolved["month_calculation_base"] = "event_date"
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 1, 16), rules=resolved)
        assert not [w for w in res.warnings if "Б5" in w]
        assert res.deadline == date(2026, 2, 16)

    def test_thirty_first_clamps_to_the_last_day_of_february(self, rules):
        # The spec's own example: 31 января + 1 месяц -> 28.02 (2026 is not a
        # leap year), which is a Saturday, so Б2 moves it to Monday 02.03.
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 1, 31), rules=rules)
        assert res.deadline == date(2026, 3, 2)
        assert any("2026-02-28" in step["step"] for step in res.trace)
        assert not [w for w in res.warnings if "Б5" in w]

    def test_monthly_term_expires_on_the_corresponding_day_number(self, rules):
        # Б5 (colleague-legal, ч.2 ст.114 АПК РФ): a term counted in months ends on
        # the "соответствующее число" of the last month — the day number of the
        # EVENT date. 16.01 -> 16.02, not 17.02.
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 1, 16), rules=rules)
        assert res.status == STATUS_OK
        assert res.deadline == date(2026, 2, 16)
        assert not [w for w in res.warnings if "Б5" in w]

    def test_monthly_term_is_one_month_not_one_month_and_a_day(self, rules):
        # The whole point of the opinion: counting from Day 1 would hand the
        # party a month plus a day, which contradicts «в соответствующее число».
        event = date(2026, 5, 15)
        res = calculate_deadline(deadline_key="as_appeal_decision",
                                 event_date=event, rules=rules)
        assert res.deadline == date(2026, 6, 15)
        assert (res.deadline - event).days == 31  # May has 31 days, June 15 is one calendar month

    def test_appeal_month_arbitration(self, rules):
        # 09.10.2026 (Fri) event -> +1 month on the event day number =
        # 09.11.2026 (Mon), a working day, so no shift applies.
        res = calculate_deadline(deadline_key="as_appeal_decision",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.status == STATUS_OK
        assert res.deadline == date(2026, 11, 9)

    def test_appeal_month_soyu(self, rules):
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.deadline == date(2026, 11, 9)

    def test_month_clamps_to_month_end_then_shifts(self, rules):
        # 30.01.2027 event -> Day 1 = 31.01.2027 -> +1 month = 28.02.2027
        # (31 Feb does not exist), which is a Sunday, so Б2 shifts it to Mon
        # 01.03.2027. Both the clamp and the shift are exercised here.
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2027, 1, 30), rules=rules)
        assert res.deadline == date(2027, 3, 1)
        assert any("2027-02-28" in step["step"] and "перенесён" in step["step"]
                   for step in res.trace)

    def test_month_from_31st_clamps_to_the_last_existing_day(self, rules):
        # 31.01.2027 event + 3 месяца: February has no 31st, so ч.2 ст.114 puts
        # the term on the last day of April — 30.04.2027, a Friday, so no shift.
        res = calculate_deadline(deadline_key="soyu_cassation",
                                 event_date=date(2027, 1, 31), rules=rules)
        assert res.status == STATUS_OK
        assert res.deadline == date(2027, 4, 30)
        assert not [w for w in res.warnings if "Б5" in w]

    def test_day1_assumption_documented_in_trace(self, rules):
        # Б1: the counter starts at the next calendar day and skips non-working
        # days without incrementing, so the trace must name Day 1 explicitly.
        res = calculate_deadline(deadline_key="as_appeal_simplified",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert any("Day 1 = 2026-10-10" in step["step"] for step in res.trace)

    def test_event_day_not_counted(self, rules):
        # Day 0 is the event; a same-day deadline would mean it was counted.
        res = calculate_deadline(deadline_key="soyu_appeal_decision",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.deadline != date(2026, 10, 9)


class TestDeadlineWorkingDays:
    def test_15_working_days(self, rules):
        # 09.10.2026 Fri -> Day 1 Mon 12.10, 15th working day = 30.10
        res = calculate_deadline(deadline_key="as_appeal_simplified",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.deadline == date(2026, 10, 30)

    def test_working_days_skip_weekend(self, rules):
        res = calculate_deadline(deadline_key="as_appeal_simplified",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.deadline.weekday() < 5

    def test_event_day_not_counted(self, rules):
        res = calculate_deadline(deadline_key="as_appeal_simplified",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.deadline.weekday() < 5


class TestCalendarWarnings:
    def test_calendar_warning_follows_the_deadline_year(self, rules):
        # The warning tracks the year the deadline lands in, not the calendar
        # as a whole: 2026 and 2027 have verified decrees, 2028 has none.
        res_2026 = calculate_deadline(deadline_key="as_appeal_decision",
                                      event_date=date(2026, 10, 9), rules=rules)
        assert res_2026.deadline is not None
        assert res_2026.deadline.year == 2026
        assert not any("CALENDAR_WARNING" in w for w in res_2026.warnings)
        assert any("1466" in w for w in res_2026.warnings)

        # 2027 is verified too — it must name decree № 1187, not warn.
        res_2027 = calculate_deadline(deadline_key="soyu_appeal_decision",
                                      event_date=date(2026, 12, 25), rules=rules)
        assert res_2027.deadline is not None
        assert res_2027.deadline.year == 2027
        assert not any("CALENDAR_WARNING" in w for w in res_2027.warnings)
        assert any("1187" in w for w in res_2027.warnings)

        # A deadline pushed into 2028 must warn: no decree for that year is
        # encoded, and the per-year gate is what stops the verified years from
        # silencing the warning on their behalf.
        res_2028 = calculate_deadline(deadline_key="soyu_cassation",
                                      event_date=date(2027, 10, 5), rules=rules)
        assert res_2028.deadline is not None
        assert res_2028.deadline.year == 2028
        assert any("CALENDAR_WARNING" in w for w in res_2028.warnings)

    def test_boundary_note_present(self, rules):
        res = calculate_deadline(deadline_key="as_appeal_decision",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert any("восстановлен" in w for w in res.warnings)

    def test_regional_note_present(self, rules):
        res = calculate_deadline(deadline_key="as_appeal_decision",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert any("Региональные" in w for w in res.warnings)

    def test_no_B5_warning_on_monthly_terms_after_the_opinion(self, rules):
        # Б5 was closed, so the monthly terms must no longer carry the warning
        # that asked the caller to expect two readings.
        res = calculate_deadline(deadline_key="soyu_cassation",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert not [w for w in res.warnings if "Б5" in w]

    def test_no_B5_warning_on_day_terms(self, rules):
        # Б5 concerns only the base of month-long terms; warning on a 10-day
        # deadline would be noise, so its absence is asserted too.
        res = calculate_deadline(deadline_key="as_court_order_cancel",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert not any("Б5" in w for w in res.warnings)


class TestDeadlineShiftOffNonWorkingDay:
    """Б2 + ч. 4 ст. 114 АПК / ч. 2 ст. 108 ГПК: shift, don't just warn."""

    def test_deadline_on_sunday_is_shifted_to_monday(self, rules):
        # 09.10.2026 + 1 month = 09.11.2026, a Monday; use a case that lands
        # on a weekend: 2 months from 09.10.2026 (ГПК calendar) -> 09.12.2026 Wed.
        # The судебный приказ case below is the concrete weekend case.
        res = calculate_deadline(deadline_key="soyu_court_order_cassation",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.deadline.weekday() < 5

    def test_shift_is_recorded_in_trace(self, rules):
        # 01.04.2026 + 10 calendar days lands on Sat 11.04 -> Mon 13.04.
        res = calculate_deadline(deadline_key="soyu_court_order_cancel",
                                 event_date=date(2026, 4, 1), rules=rules)
        assert res.deadline == date(2026, 4, 13)
        assert any("перенесён" in step["step"] for step in res.trace)

    def test_shift_emits_warning(self, rules):
        res = calculate_deadline(deadline_key="soyu_court_order_cancel",
                                 event_date=date(2026, 4, 1), rules=rules)
        assert any("нерабочий день" in w and "перенесён" in w for w in res.warnings)

    def test_no_shift_needed_on_working_day(self, rules):
        # A deadline already on a working day must not claim a shift happened.
        res = calculate_deadline(deadline_key="soyu_court_order_cancel",
                                 event_date=date(2026, 4, 25), rules=rules)
        assert not any("перенесён" in step["step"] for step in res.trace)

    def test_result_never_lands_on_a_non_working_day(self, rules):
        for key in ("as_appeal_decision", "soyu_appeal_decision",
                    "soyu_court_order_cancel", "kas_appeal_decision"):
            res = calculate_deadline(deadline_key=key,
                                     event_date=date(2026, 10, 9), rules=rules)
            assert is_working_day(res.deadline), key


class TestDeadlineFailClosed:
    def test_unknown_key_is_manual_review(self, rules):
        # A key that was never in the matrix must still be refused rather than
        # invented. (kas_cassation used to live here, but addendum Б3 added it.)
        res = calculate_deadline(deadline_key="kas_cassation_never_defined",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.status == STATUS_MANUAL_REVIEW
        assert res.deadline is None

    def test_missing_event_date(self, rules):
        res = calculate_deadline(deadline_key="as_appeal_decision", rules=rules)
        assert res.status == STATUS_MISSING_PARAM

    def test_court_order_deadlines_now_present(self, rules):
        # Б3 added these rows; they must compute rather than refuse.
        for key in ("as_court_order_cancel", "soyu_court_order_cancel",
                    "kas_court_order_cancel"):
            res = calculate_deadline(deadline_key=key,
                                     event_date=date(2026, 10, 9), rules=rules)
            assert res.status == STATUS_OK, key
            assert res.deadline is not None

    def test_shortened_special_deadline_is_manual_review(self, rules):
        res = calculate_deadline(deadline_key="shortened_special_deadline",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.status == STATUS_MANUAL_REVIEW
        assert "сокращённые" in res.detail

    def test_genuinely_unknown_key_is_manual_review(self, rules):
        res = calculate_deadline(deadline_key="soyu_something_invented",
                                 event_date=date(2026, 10, 9), rules=rules)
        assert res.status == STATUS_MANUAL_REVIEW
        assert res.deadline is None


class TestMatrixIntegrity:
    def test_every_row_has_legal_basis(self, rules):
        for key, entry in rules["deadline_matrix"].items():
            assert entry["legal_basis"], key
            assert entry["jurisdiction"] in rules["jisdictions"], key
            if entry.get("always_manual_review"):
                assert entry.get("duration") is None, key
            else:
                assert entry["duration"]["value"] > 0, key

    def test_jurisdiction_day_mode_used(self, rules):
        assert rules["jisdictions"]["arbitration"]["deadline_day_mode"] == "working_days"
        assert rules["jisdictions"]["general_jurisdiction"]["deadline_day_mode"] == "calendar_days"
        assert rules["jisdictions"]["kas"]["deadline_day_mode"] == "calendar_days"

    def test_kas_uses_333_19_tariffs(self, rules):
        # Б4: КАС РФ is charged under ст. 333.19 НК РФ in full.
        kas = rules["jisdictions"]["kas"]
        soyu = rules["jisdictions"]["general_jurisdiction"]
        assert kas["fee"]["appeal"]["individual"] == soyu["fee"]["appeal"]["individual"]
        assert kas["fee"]["cassation_first"]["entity"] == soyu["fee"]["cassation_first"]["entity"]

    def test_addendum_rows_are_present(self, rules):
        for key in ("as_court_order_cancel", "soyu_court_order_cancel",
                    "kas_court_order_cancel", "as_court_order_cassation",
                    "soyu_court_order_cassation", "kas_appeal_decision",
                    "kas_cassation", "as_appeal_ruling", "soyu_appeal_ruling"):
            assert key in rules["deadline_matrix"], key


class TestNoPiiInOutput:
    """Clean-room: the engine receives abstract parameters only."""

    def test_result_carries_no_free_text_input(self, rules):
        res = calculate_fee(claim_amount=500_000, jurisdiction="arbitration",
                            fee_kind="property_claim", rules=rules)
        blob = json.dumps(res.to_dict(), ensure_ascii=False)
        for forbidden in ("паспорт", "ФИО", "истец", "ответчик"):
            assert forbidden not in blob

    def test_deadline_result_is_structured(self, rules):
        res = calculate_deadline(deadline_key="as_appeal_decision",
                                 event_date=date(2026, 10, 9), rules=rules)
        payload = res.to_dict()
        assert set(payload) == {
            "status", "deadline", "detail", "legal_basis", "warnings", "trace"
        }
