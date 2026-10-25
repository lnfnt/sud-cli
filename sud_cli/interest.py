"""Interest calculator for articles 395 and 317.1 of the Civil Code."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

#: v2.3.2 (sa-1 F-11): embedded-копия остаётся fail-safe фолбэком;
#: боевой источник — data/key_rates.json (обновляется без правки кода).
_RATE_PERIODS_EMBEDDED = (
    (date(2026, 6, 15), Decimal("18")),
    (date(2026, 2, 16), Decimal("15")),
    (date(2025, 12, 22), Decimal("16")),
    (date(2025, 9, 15), Decimal("17")),
    (date(2025, 6, 9), Decimal("20")),
    (date(2024, 12, 16), Decimal("21")),
    (date(2024, 10, 28), Decimal("21")),
    (date(2024, 9, 16), Decimal("19")),
    (date(2024, 7, 29), Decimal("18")),
    (date(2024, 6, 10), Decimal("16")),
    (date(2024, 4, 29), Decimal("16")),
    (date(2023, 9, 18), Decimal("13")),
    (date(2023, 7, 24), Decimal("8.5")),
)

_RATES_PATH = __import__("pathlib").Path(__file__).with_name("data") / "key_rates.json"


def _load_rate_periods():
    """JSON-first загрузка с embedded-фолбэком (нет файла — не падаем)."""
    try:
        data = json.loads(_RATES_PATH.read_text(encoding="utf-8"))
        periods = tuple((date.fromisoformat(d), Decimal(str(r))) for d, r in data["periods"])
        if periods:
            return periods, data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _RATE_PERIODS_EMBEDDED, {"provenance": "embedded fallback (key_rates.json missing/unreadable)",
                                    "coverage_latest_change": _RATE_PERIODS_EMBEDDED[0][0].isoformat(),
                                    "coverage_first": _RATE_PERIODS_EMBEDDED[-1][0].isoformat()}


RATE_PERIODS, RATES_META = _load_rate_periods()


def rate_on(day):
    for starts, rate in RATE_PERIODS:
        if day >= starts:
            return rate
    return Decimal("7.5")


@dataclass
class InterestResult:
    status: str
    amount_rub: Decimal = Decimal("0")
    days: int = 0
    periods: list = None
    article: str = "395"
    remaining_principal: Decimal = Decimal("0")
    rates_warnings: list = None
    rates_meta: dict = None

    def to_dict(self):
        out = {"status": self.status, "article": self.article, "amount_rub": float(self.amount_rub), "days": self.days, "remaining_principal_rub": float(self.remaining_principal), "periods": self.periods or []}
        if self.rates_warnings:
            out["rates_warnings"] = self.rates_warnings
        if self.rates_meta:
            out["rates_meta"] = self.rates_meta
        return out


def parse_payments(value):
    """Parse ``date:amount,...`` or a JSON list of payment objects."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.lstrip().startswith("[") else [
                dict(zip(("date", "amount"), item.split(":", 1)))
                for item in value.split(",")
            ]
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
    if not isinstance(value, list):
        return None
    payments = []
    try:
        for item in value:
            if isinstance(item, (list, tuple)):
                item = {"date": item[0], "amount": item[1]}
            day = date.fromisoformat(item.get("date"))
            paid = Decimal(str(item.get("amount")))
            if paid <= 0:
                return None
            payments.append((day, paid))
    except (AttributeError, IndexError, TypeError, ValueError, ArithmeticError):
        return None
    return sorted(payments)


def calculate_interest(amount, start, end, article="395", payments=None):
    if amount <= 0 or end <= start:
        return InterestResult("MISSING_REQUIRED_PARAM", article=article)
    parsed_payments = parse_payments(payments)
    if parsed_payments is None or any(day < start or day > end for day, _ in parsed_payments):
        return InterestResult("MISSING_REQUIRED_PARAM", article=article)
    principal = Decimal(str(amount))
    payments_by_day = {}
    for day, paid in parsed_payments:
        payments_by_day[day] = payments_by_day.get(day, Decimal("0")) + paid
    if any(sum((paid for day, paid in parsed_payments if day <= point), Decimal("0")) > principal for point in payments_by_day):
        return InterestResult("MISSING_REQUIRED_PARAM", article=article)
    cursor, total, periods = start, Decimal("0"), []
    while cursor < end:
        principal -= payments_by_day.get(cursor, Decimal("0"))
        rate = rate_on(cursor)
        boundaries = [d for d, _ in RATE_PERIODS if cursor < d < end]
        boundaries.extend(d for d in payments_by_day if cursor < d < end)
        # v2.3.3 (review R5): период через Новый год считал все дни по
        # year_days года старта — 366 для невисокосных отрезков занижало проценты.
        yb = date(cursor.year + 1, 1, 1)
        if cursor < yb < end:
            boundaries.append(yb)
        stop = min(boundaries, default=end)
        days = (stop - cursor).days
        year_days = 366 if (cursor.year % 4 == 0 and (cursor.year % 100 != 0 or cursor.year % 400 == 0)) else 365
        value = principal * days / year_days * rate / 100
        total += value
        periods.append({"from": cursor.isoformat(), "to": stop.isoformat(), "days": days, "rate": float(rate), "principal_rub": float(principal), "amount_rub": float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))})
        cursor = stop
    remaining = principal - payments_by_day.get(end, Decimal("0"))
    result = InterestResult("ok", total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), (end - start).days, periods, article, remaining)
    # v2.3.2 (sa-1 F-11): тихая экстраполяция за границу покрытия — теперь с предупреждением.
    warnings = []
    latest = RATES_META.get("coverage_latest_change")
    if latest and end.isoformat() > latest:
        warnings.append(f"rates after {latest} extrapolated from last known key rate")
    if not RATES_META.get("periods_source", True) or "embedded fallback" in str(RATES_META.get("provenance", "")):
        warnings.append("key rates loaded from embedded fallback, not key_rates.json")
    result.rates_warnings = warnings
    result.rates_meta = {"provenance": RATES_META.get("provenance"), "coverage_latest_change": latest,
                         "coverage_first": RATES_META.get("coverage_first")}
    return result
