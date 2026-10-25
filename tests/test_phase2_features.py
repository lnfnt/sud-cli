import json
from datetime import date

from tests.conftest import run_cli
from sud_cli.interest import calculate_interest
from sud_cli.legal_deadlines import calculate_deadline
from sud_cli.production_calendar import calendar_check, is_working_day


def test_regional_holiday_changes_working_day_and_deadline_trace():
    assert not is_working_day(date(2026, 11, 6), region="16")
    result = calculate_deadline(
        deadline_key="soyu_court_order_cancel",
        event_date=date(2026, 8, 19),
        region="16",
    )
    assert result.region == "16"
    assert any("Татарстан" in warning for warning in result.warnings)


def test_federal_calendar_warns_when_region_is_omitted():
    result = calculate_deadline(
        deadline_key="as_appeal_decision", event_date=date(2026, 10, 9)
    )
    assert any("укажите --region" in warning for warning in result.warnings)


def test_deadline_has_local_end_of_day_when_timezone_requested():
    result = calculate_deadline(
        deadline_key="as_appeal_decision",
        event_date=date(2026, 10, 9),
        tz="Asia/Tomsk",
    )
    assert result.deadline_datetime.endswith("23:59:59+07:00")
    assert "Asia/Tomsk" in result.time_with_tz


def test_calendar_check_reports_verified_years():
    report = calendar_check()
    assert report["valid"]
    assert report["verified_through"] == 2027
    assert report["years"] == [2026, 2027]


def test_calendar_and_doctor_commands_are_healthy(tmp_path):
    code, out, _ = run_cli(["--json", "calendar", "check"])
    assert code == 0
    assert json.loads(out)["data"]["valid"]
    code, out, _ = run_cli(["--json", "--state-dir", str(tmp_path), "doctor"])
    assert code == 0
    assert json.loads(out)["data"]["calendar"]["valid"]


def test_interest_splits_rate_periods_and_uses_leap_year():
    result = calculate_interest(100000, date(2024, 10, 27), date(2024, 10, 29))
    assert result.status == "ok"
    assert result.days == 2
    assert len(result.periods) == 2
    assert round(float(result.amount_rub), 2) == round(100000 * (1 / 366) * 19 / 100 + 100000 * (1 / 366) * 21 / 100, 2)


def test_interest_cli_can_calculate_fee_from_claim_price():
    code, out, _ = run_cli([
        "--json", "interest", "--amount", "100000", "--from", "2026-01-01",
        "--to", "2026-01-31", "--calc-fee", "--jurisdiction", "arbitration",
        "--kind", "property_claim", "--applicant", "entity",
    ])
    data = json.loads(out)
    assert code == 0
    assert data["data"]["claim_amount_rub"] > 100000
    assert data["data"]["fee"]["status"] == "ok"


def test_payment_and_deferral_commands_render_required_text():
    code, out, _ = run_cli([
        "--json", "payment", "--jurisdiction", "arbitration", "--amount", "10000",
        "--court", "АС города Москвы", "--defendant", "ООО Ромашка",
    ])
    assert code == 0
    payment = json.loads(out)["data"]
    assert payment["kbk"]
    assert "Без НДС" in payment["purpose"]
    code, out, _ = run_cli([
        "--json", "deferral", "--applicant", "ООО Ромашка", "--court", "АС",
        "--amount", "100000", "--reason", "отсутствием денежных средств",
    ])
    assert code == 0
    text = json.loads(out)["data"]["text"]
    assert "333.41" in text and "Пленума ВАС РФ № 20" in text and "картотеки № 2" in text
