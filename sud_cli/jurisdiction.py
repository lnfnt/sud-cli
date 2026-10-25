"""Jurisdiction venue helper for sud-cli (v2.6.0).

Pure read of sud_cli/jurisdiction_rules.json. No norm texts are invented:
reference_only entries carry fetch_hint="npa_as_of" instead of legal_text.
"""
import json
from pathlib import Path

RULES_PATH = Path(__file__).with_name("jurisdiction_rules.json")

CODE_TO_KEY = {"apk": "APK", "gpk": "GPK", "kas": "KAS"}


def load_rules(path=None):
    return json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))


def _candidate(rule):
    out = {
        "court_level": _court_level(rule),
        "venue_basis": rule["norm"],
        "confidence": rule["confidence"],
    }
    if rule.get("legal_text"):
        out["legal_text"] = rule["legal_text"]
    else:
        out["fetch_hint"] = "npa_as_of"
    return out


def _court_level(rule):
    effect = rule.get("effect", "")
    return effect.split(",")[0].strip() if effect else rule.get("norm", "")


def _mr_entry(rule):
    return {"reason": rule.get("mr_reason") or rule.get("effect", ""), "norm": rule["norm"]}


_FLAG_KEYS = {
    "APK": {
        "unknown_rf_location": ["unknown_or_no_ru_loc_36_1"],
        "immovable": ["exclusive_38"],
        "corporate": ["corporate_38_4_1"],
        "bankruptcy": ["bankruptcy"],
        "ip_rights": ["ip_court"],
        "contract_forum": ["agree_37_2"],
        "arbitration_clause": None,
        "multiple_defendants": ["multiple_defendants_36_2"],
        "foreign_element": ["foreign_element"],
    },
    "GPK": {
        "unknown_rf_location": ["plaintiff_choice_29"],
        "consumer": ["plaintiff_choice_29"],
        "alimony": ["plaintiff_choice_29"],
        "immovable": ["exclusive_30"],
        "contract_forum": ["agree_32"],
        "arbitration_clause": None,
        "multiple_defendants": ["multiple_31_1"],
        "foreign_element": ["foreign_element"],
    },
    "KAS": {
        "unknown_rf_location": ["choice_24"],
    },
}


def _claim_has(claim, *words):
    cl = str(claim or "").lower()
    return any(w in cl for w in words)


def check_jurisdiction(code, claim=None, party_org=False, party_phys=False,
                      unknown_rf_location=False, immovable=False, corporate=False,
                      bankruptcy=False, ip_rights=False, contract_forum=False,
                      arbitration_clause=False, multiple_defendants=False,
                      foreign_element=False, consumer=False, alimony=False,
                      rules=None):
    # Claim keywords can also drive branches (keeps --claim meaningful).
    unknown_rf_location = unknown_rf_location or _claim_has(claim, "unknown", "неизвест", "no_ru", "no-location", "unknown_rf_location", "unknown_rf")
    immovable = immovable or _claim_has(claim, "immovable", "недвиж", "имущ")
    corporate = corporate or _claim_has(claim, "corporate", "корпор")
    bankruptcy = bankruptcy or _claim_has(claim, "bankruptcy", "банкрот")
    ip_rights = ip_rights or _claim_has(claim, "ip_rights", "ip-rights", "интеллект", "сип", "sip", "intellectual", "ip")
    contract_forum = contract_forum or _claim_has(claim, "contract", "договор", "forum", "форум")
    arbitration_clause = arbitration_clause or _claim_has(claim, "arbitration", "арбитраж", "третей", "оговорк")
    multiple_defendants = multiple_defendants or _claim_has(claim, "multiple", "нескольк", "множ")
    foreign_element = foreign_element or _claim_has(claim, "foreign", "иностр", "трансгран")
    consumer = consumer or _claim_has(claim, "consumer", "потреб")
    alimony = alimony or _claim_has(claim, "alimony", "алимент")

    rules = rules or load_rules()
    key = CODE_TO_KEY.get(str(code).lower(), str(code).upper())
    jur = rules["jurisdiction"].get(key, {})
    flags = {
        "unknown_rf_location": unknown_rf_location,
        "immovable": immovable,
        "corporate": corporate,
        "bankruptcy": bankruptcy,
        "ip_rights": ip_rights,
        "contract_forum": contract_forum,
        "arbitration_clause": arbitration_clause,
        "multiple_defendants": multiple_defendants,
        "foreign_element": foreign_element,
        "consumer": consumer,
        "alimony": alimony,
    }

    candidates = []
    manual_review = []

    general_key = {
        "APK": "general_35",
        "GPK": "general_28",
        "KAS": "general_organ_22",
    }.get(key)
    if general_key and general_key in jur:
        candidates.append(_candidate(jur[general_key]))

    mapping = _FLAG_KEYS.get(key, {})
    for flag, rkeys in mapping.items():
        if not flags.get(flag):
            continue
        if rkeys is None:
            continue
        for rk in rkeys:
            if rk in jur:
                rule = jur[rk]
                if rule.get("confidence") == "manual_review":
                    manual_review.append(_mr_entry(rule))
                else:
                    cand = _candidate(rule)
                    if cand not in candidates:
                        candidates.append(cand)

    if arbitration_clause:
        aa = rules.get("arbitration_agreement", {}).get(str(code).lower(), {})
        if aa:
            manual_review.append(_mr_entry(aa))

    # Claim-driven specialized rules.
    cl = str(claim or "").lower()
    if key == "GPK" and any(w in cl for w in ("spec", "спец", "воен", "military", "spec_military", "специализ", "военный")):
        if "spec_military" in jur:
            manual_review.append(_mr_entry(jur["spec_military"]))
    if key == "KAS" and any(w in cl for w in ("vs_first", "верхов", "ст.21", "ст21", "первая", "first", "21", "vsrf", "вс рф", "вс_рф", "верховный")):
        if "vs_first_instance_21" in jur:
            rule = jur["vs_first_instance_21"]
            if rule.get("confidence") == "manual_review":
                manual_review.append(_mr_entry(rule))
            else:
                cand = _candidate(rule)
                if cand not in candidates:
                    candidates.append(cand)
    # Foreign via claim already sets flag; ensure manual_review entry present.
    if foreign_element and "foreign_element" in jur and jur["foreign_element"].get("confidence") == "manual_review":
        pass

    # Dedup manual_review by norm.
    seen = set()
    deduped = []
    for mr in manual_review:
        if mr["norm"] in seen:
            continue
        seen.add(mr["norm"])
        deduped.append(mr)
    manual_review = deduped

    warnings = [rules.get("disclaimer", "")]
    return {
        "code": str(code).lower(),
        "claim": claim,
        "candidates": candidates,
        "manual_review": manual_review,
        "warnings": warnings,
        "disclaimer": rules.get("disclaimer", ""),
    }
