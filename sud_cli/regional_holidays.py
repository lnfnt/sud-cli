"""Fixed regional non-working holidays used as an optional calendar layer."""

from datetime import date

REGIONS = {
    "02": {"name": "Республика Башкортостан", "holidays": {"03-17": "День Конституции", "10-11": "День Республики"}},
    "03": {"name": "Республика Бурятия", "holidays": {"07-11": "День Республики"}},
    "05": {"name": "Республика Дагестан", "holidays": {"09-15": "День единства народов Дагестана"}},
    "14": {"name": "Республика Саха (Якутия)", "holidays": {"04-27": "День Республики"}},
    "16": {"name": "Республика Татарстан", "holidays": {"08-30": "День Республики Татарстан", "11-06": "День Конституции Татарстана"}},
    "20": {"name": "Чеченская Республика", "holidays": {"09-06": "День гражданского согласия и единения"}},
    "91": {"name": "Республика Крым", "holidays": {"03-18": "День воссоединения с Россией", "09-20": "День Республики Крым"}},
    "77": {"name": "Федеральный календарь", "holidays": {}},
}


def normalize_region(region):
    if region is None:
        return None
    code = str(region).strip()
    if code == "9":
        code = "09"
    if code not in REGIONS:
        raise ValueError(f"unknown region code: {region}")
    return code


def holidays_for_year(year, region=None):
    code = normalize_region(region)
    if code is None or code == "77":
        return set()
    return {date(year, int(token[:2]), int(token[3:])) for token in REGIONS[code]["holidays"]}


def region_name(region):
    code = normalize_region(region)
    return REGIONS[code]["name"] if code else None
