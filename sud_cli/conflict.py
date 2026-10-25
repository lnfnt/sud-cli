"""Conflict-of-interest heuristic for sud-cli (v2.4.1).

Pure local heuristic: name-exact intersection of party names across the local
case registry. No external parties/affiliates are known, so absence of a hit
does NOT mean absence of a conflict.
"""

from collections import defaultdict
from typing import Any, Dict, List

METHOD = "name_exact"
COVERAGE = "local registry names only"
DISCLAIMER = (
    "Отсутствие совпадений не означает отсутствие конфликта: покрытие — "
    "только имена локального реестра; внешние стороны и аффилированные лица "
    "не охвачены. Решение остаётся за человеком (boundary IX-3)."
)


def normalize_name(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    for q in ('"', "'", "«", "»", "„", "“", "”", "`"):
        text = text.replace(q, "")
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = " ".join(text.split())
    return text.upper()


# Aliases for callers that expect other names.
normalize = normalize_name
normalize_party = normalize_name


def _case_id(case: Any, fallback: Any = None) -> Any:
    if isinstance(case, dict):
        for key in ("case_id", "id", "case_number", "number"):
            v = case.get(key)
            if v is not None and str(v).strip() != "":
                return v
    return fallback


def _owner_key(case: Any) -> Any:
    if not isinstance(case, dict):
        return None
    val = case.get("owner")
    if val is None or str(val).strip() == "":
        return None
    return normalize_name(val)


def _iter_roles(case: Any):
    if not isinstance(case, dict):
        return
    for role in ("plaintiff", "defendant", "owner"):
        val = case.get(role)
        if val is not None and str(val).strip() != "":
            yield normalize_name(val), role, val
    parties = case.get("parties")
    if isinstance(parties, dict):
        for role, val in parties.items():
            if val is not None and str(val).strip() != "":
                yield normalize_name(val), str(role), val
    elif isinstance(parties, (list, tuple)):
        for val in parties:
            if val is not None and str(val).strip() != "":
                yield normalize_name(val), "party", val


def _collect_candidate_parties(candidate: Any) -> List[str]:
    parties: List[str] = []
    if isinstance(candidate, dict):
        raw = candidate.get("parties")
        if isinstance(raw, (list, tuple)):
            parties.extend(str(p) for p in raw)
        elif isinstance(raw, dict):
            parties.extend(str(v) for v in raw.values() if v is not None and str(v).strip() != "")
        elif isinstance(raw, str):
            parties.append(raw)
        for key in ("plaintiff", "defendant", "party", "name", "owner"):
            val = candidate.get(key)
            if val is not None and str(val).strip() != "":
                parties.append(str(val))
    elif isinstance(candidate, (list, tuple)):
        parties.extend(str(p) for p in candidate)
    elif candidate is not None:
        parties.append(str(candidate))
    return parties


def check(portfolio: Any, candidate: Any) -> List[Dict[str, Any]]:
    cases = list(portfolio or [])
    cand = candidate if isinstance(candidate, dict) else {"parties": candidate}
    # Exclude the candidate's own case from the portfolio when identified.
    cid = cand.get("case_id") if isinstance(cand, dict) else None
    if cid is None and isinstance(cand, dict):
        cid = cand.get("id")
    if cid is not None:
        cases = [c for c in cases if _case_id(c) != cid]

    candidate_names = []
    seen_names = set()
    for p in _collect_candidate_parties(cand):
        n = normalize_name(p)
        if n and n not in seen_names:
            seen_names.add(n)
            candidate_names.append(n)

    hits: List[Dict[str, Any]] = []

    # (a) same party in cases owned by different owners
    name_entries: Dict[str, List[Any]] = defaultdict(list)
    for case in cases:
        for norm, role, raw in _iter_roles(case):
            name_entries[norm].append((case, role))
    for name, entries in name_entries.items():
        owners = {_owner_key(c) for c, _ in entries}
        distinct = {o for o in owners if o is not None}
        if len(distinct) >= 2:
            case_ids = []
            seen = set()
            for case, _ in entries:
                k = _case_id(case)
                if k not in seen:
                    seen.add(k)
                    case_ids.append(k)
            hits.append({
                "type": "a",
                "hit_type": "a",
                "party": name,
                "normalized": name,
                "reason": "same party appears in cases owned by different owners",
                "case_ids": case_ids,
                "cases": case_ids,
                "owners": sorted(distinct),
                "method": METHOD,
            })

    # (b) adversary flip: plaintiff in one case, defendant in another, same owner
    by_owner: Dict[Any, List[Any]] = defaultdict(list)
    for case in cases:
        by_owner[_owner_key(case)].append(case)
    for owner_key, group in by_owner.items():
        if owner_key is None:
            continue
        name_to_p: Dict[str, List[Any]] = defaultdict(list)
        name_to_d: Dict[str, List[Any]] = defaultdict(list)
        for case in group:
            for norm, role, raw in _iter_roles(case):
                if role == "plaintiff":
                    name_to_p[norm].append(case)
                elif role == "defendant":
                    name_to_d[norm].append(case)
        for name in name_to_p:
            if name not in name_to_d:
                continue
            p_cases = name_to_p[name]
            d_cases = name_to_d[name]
            if not any(_case_id(pc) != _case_id(dc) for pc in p_cases for dc in d_cases):
                continue
            case_ids = []
            seen = set()
            for case in p_cases + d_cases:
                k = _case_id(case)
                if k not in seen:
                    seen.add(k)
                    case_ids.append(k)
            hits.append({
                "type": "b",
                "hit_type": "b",
                "party": name,
                "normalized": name,
                "reason": "party is plaintiff in one case and defendant in another within same owner portfolio",
                "owner": owner_key,
                "case_ids": case_ids,
                "cases": case_ids,
                "method": METHOD,
            })

    # (c) candidate party equals any party of any existing case
    existing_names = set()
    for case in cases:
        for norm, role, raw in _iter_roles(case):
            existing_names.add(norm)
    for cand_name in candidate_names:
        if cand_name not in existing_names:
            continue
        matched_ids = []
        seen = set()
        for case in cases:
            if any(norm == cand_name for norm, role, raw in _iter_roles(case)):
                k = _case_id(case)
                if k not in seen:
                    seen.add(k)
                    matched_ids.append(k)
        hits.append({
            "type": "c",
            "hit_type": "c",
            "party": cand_name,
            "name": cand_name,
            "normalized": cand_name,
            "reason": "candidate party equals a party of an existing case",
            "case_ids": matched_ids,
            "cases": matched_ids,
            "method": METHOD,
        })

    return hits
