"""Payment details and purpose text for state fees."""

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
    amount = int(amount)
    return {
        "status": "ok",
        "kbk": KBK[key],
        "eks": "Единый казначейский счет",
        "jurisdiction": jurisdiction,
        "amount_rub": amount,
        "purpose": f"Государственная пошлина за рассмотрение искового заявления в {court} к {defendant}. Сумма: {amount} руб. Без НДС.",
    }
