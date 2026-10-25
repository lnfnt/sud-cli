"""Pretrial (claim) helper for sud-cli (v2.6.0).

Pure read of sud_cli/jurisdiction_rules.json. Calendar arithmetic via
production_calendar/holidays_rf.
"""
import json
from datetime import date, timedelta
from pathlib import Path

RULES_PATH = Path(__file__).with_name("jurisdiction_rules.json")

CODE_TO_KEY = {"apk": "APK_30d", "zozpp": "ZOZPP_10d", "osago": "OSAGO_30d"}
CHARTER_CODES = {
    "railway": "railway_ujt", "ujt": "railway_ujt",
    "road": "road_uat", "uat": "road_uat",
    "maritime": "maritime_ktm", "ktm": "maritime_ktm",
    "inland": "inland_water_kvvt", "kvvt": "inland_water_kvvt",
    "air": "air_code_vk", "vk": "air_code_vk",
    "freight": "freight_forwarding", "expedition": "freight_forwarding",
    "postal": "postal",
}


def load_rules(path=None):
    return json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))


def _shift_to_working(day):
    from .production_calendar import is_working_day
    cur = day
    while not is_working_day(cur):
        cur = cur + timedelta(days=1)
    return cur


def calc_pretrial(code, sent, rules=None):
    rules = rules or load_rules()
    key = CODE_TO_KEY.get(str(code).lower())
    mode = str(code).lower()
    if mode in CHARTER_CODES:
        key = CHARTER_CODES[mode]
        rule = rules["pretrial"].get("charters_transport", {}).get(key, {})
        if not rule:
            return {"code": mode, "error": "charter_rule_absent",
                    "warnings": ["нет записи в VERIFIED rules — manual_review"]}
    else:
        rule = rules["pretrial"].get(CODE_TO_KEY.get(mode, ""), {})
    days = int(rule.get("days", 0))
    sent_d = date.fromisoformat(sent) if isinstance(sent, str) else sent
    base_due = sent_d + timedelta(days=days)
    exclude = bool(rule.get("exclude_nonworking_holidays"))
    if exclude:
        due = _shift_to_working(base_due)
    else:
        due = base_due
    right_to_sue = due
    # VERIFIED charter-нормы (юридическая проверка 2026.10.24): postal — manual_review (30/5), остальные hard
    mr = []
    if rule.get("confidence") == "manual_review" or rule.get("status") == "NOT_SERIALIZED_NEEDS_TEXT":
        mr.append({"reason": rule.get("confidence", "manual_review"),
                   "norm": rule.get("norms") or rule.get("norm", ""),
                   "scope": rule.get("scope", "")})
    warnings = ["disclaimer: " + rules.get("disclaimer", "")]
    payload = {
        "code": str(code).lower(),
        "rule": key,
        "norm": rule.get("norm") or rule.get("norms", ""),
        "sent": sent_d.isoformat(),
        "sent_date": sent_d.isoformat(),
        "days": days,
        "calendar": bool(rule.get("calendar", True)),
        "exclude_nonworking_holidays": exclude,
        "response_deadline": due.isoformat(),
        "response_deadline_date": due.isoformat(),
        "answer_deadline": due.isoformat(),
        "deadline": due.isoformat(),
        "deadline_date": due.isoformat(),
        "due_date": due.isoformat(),
        "due": due.isoformat(),
        "right_to_sue_date": right_to_sue.isoformat(),
        "right_to_sue": right_to_sue.isoformat(),
        "right_to_claim_date": right_to_sue.isoformat(),
        "right_to_claim": right_to_sue.isoformat(),
        "sue_date": right_to_sue.isoformat(),
        "claim_date": right_to_sue.isoformat(),
        "warnings": warnings,
        "manual_review": mr,
        "disclaimer": rules.get("disclaimer", ""),
    }
    return payload


def consequences(code, stage, rules=None):
    rules = rules or load_rules()
    code_u = str(code).upper()
    cons = rules["consequences_noncompliance"]
    stage_map = {
        "filing": "return_at_filing",
        "trial": "left_without_consideration",
        "movement": "left_without_movement",
    }
    cons_key = stage_map.get(str(stage).lower(), str(stage).lower())

    norms = {}
    for k, v in cons.items():
        if k == "confidence":
            continue
        if isinstance(v, dict) and code_u in v:
            norms[k] = v[code_u]

    selected = cons.get(cons_key, {})
    selected_norm = None
    basis = None
    confidence = "hard"
    if isinstance(selected, dict) and code_u in selected:
        selected_val = selected[code_u]
        if isinstance(selected_val, dict):
            selected_norm = selected_val.get("norm", selected_val)
            basis = selected_val.get("basis")
        else:
            selected_norm = selected_val
        confidence = selected.get("confidence", confidence)
    elif isinstance(selected, dict):
        selected_norm = None
        basis = None
        confidence = selected.get("confidence", confidence)
    else:
        selected_norm = selected

    lwm_full = cons.get("left_without_movement", {}).get(code_u)
    if isinstance(lwm_full, dict):
        norms["left_without_movement"] = lwm_full

    out = {
        "code": str(code).lower(),
        "stage": str(stage).lower(),
        "consequence": cons_key,
        "norm": selected_norm,
        "norms": norms,
        "return_at_filing": norms.get("return_at_filing"),
        "left_without_consideration": norms.get("left_without_consideration"),
        "left_without_movement": lwm_full if isinstance(lwm_full, dict) else norms.get("left_without_movement"),
        "confidence": confidence,
        "disclaimer": rules.get("disclaimer", ""),
        "warnings": [rules.get("disclaimer", "")],
    }
    if basis:
        out["basis"] = basis
    if isinstance(lwm_full, dict) and "basis" in lwm_full and "basis" not in out and cons_key == "left_without_movement":
        out["basis"] = lwm_full["basis"]
    return out
