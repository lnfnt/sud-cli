# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Payment details and purpose text for state fees."""

from decimal import Decimal, InvalidOperation

KBK = {
    "eks": "182 1 08 01000 01 1050 110",
    "arbitration": "182 1 08 01000 01 1050 110",
    "general_jurisdiction": "182 1 08 03010 01 1050 110",
    "soyu": "182 1 08 03010 01 1050 110",
    "supreme_court": "182 1 08 03000 01 1050 110",
}


def payment_details(jurisdiction, amount, court, defendant):
    key = "general_jurisdiction" if jurisdiction in {"soyu", "general_jurisdiction", "kas"} else jurisdiction
    if key not in KBK:
        raise ValueError(f"unknown jurisdiction: {jurisdiction}")
    try:
        dec = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return {"status": "INVALID_AMOUNT", "reason": f"amount must be a number, got {amount!r}",
                "kbk": KBK[key], "jurisdiction": jurisdiction, "amount_rub": None,
                "purpose": ""}
    if not dec.is_finite() or dec <= 0:
        return {"status": "INVALID_AMOUNT", "reason": "amount must be positive",
                "kbk": KBK[key], "jurisdiction": jurisdiction, "amount_rub": None,
                "purpose": ""}
    if dec == dec.to_integral_value():
        amount_out = int(dec)
    else:
        amount_out = float(dec.quantize(Decimal("0.01")))
    return {
        "status": "ok",
        "kbk": KBK[key],
        "eks": "Единый казначейский счет",
        "jurisdiction": jurisdiction,
        "amount_rub": amount_out,
        "purpose": f"Государственная пошлина за рассмотрение искового заявления в {court} к {defendant}. Сумма: {amount_out} руб. Без НДС.",
    }
