"""Deadline matrix (control points) for a case (sud v2.7.0).

Builds every applicable point from the approved ``deadline_matrix`` (legal_rules)
without inventing base dates: a point without an anchored/assumed base event
gets ``due_at=None`` and ``needs_anchor=True`` (правило «тексты только из реестра»).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .db import get_case, get_connection, get_latest_deadline_state, init_db
from .legal_deadlines import calculate_deadline
from .legal_engine import STATUS_OK, load_rules


INSTANCE_TO_JURISDICTION = {
    "apk": "arbitration",
    "gpk": "general_jurisdiction",
    "kas": "kas",
}


def _basis_event_and_date(latest: Optional[dict]) -> Tuple[str, Optional[date]]:
    if not latest:
        return "MISSING", None
    basis = latest.get("basis")
    if not isinstance(basis, dict):
        return "MISSING", None
    raw = basis.get("event_date")
    if not raw:
        return "MISSING", None
    try:
        d = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return "MISSING", None
    if "method" in basis:
        return "anchored", d
    if basis.get("assumed") is True:
        return "assumed", d
    return "anchored", d


def build_matrix(
    state_dir: Any,
    case_id: str,
    instance_override: Optional[str] = None,
    rules: Optional[dict] = None,
    as_of: Optional[str] = None,
) -> Tuple[str, List[dict]]:
    rules = rules or load_rules()
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        case = get_case(conn, case_id)
        if case is None:
            raise ValueError(f"case not found: {case_id}")
        instance = (instance_override or case.get("instance") or "unknown")
        instance = str(instance).strip().lower()
        if instance not in ("apk", "gpk", "kas", "unknown"):
            raise ValueError(f"invalid instance: {instance}")

        matrix = rules.get("deadline_matrix", {}) or {}
        if instance in INSTANCE_TO_JURISDICTION:
            target = INSTANCE_TO_JURISDICTION[instance]
            keys = [k for k, v in matrix.items() if (v or {}).get("jurisdiction") == target]
        else:
            keys = list(matrix.keys())

        points: List[dict] = []
        for key in keys:
            entry = matrix.get(key) or {}
            latest = get_latest_deadline_state(conn, case_id, key)
            state_val = latest.get("state") if latest else None
            base_event, base_date = _basis_event_and_date(latest)

            due_at = None
            if base_date is not None:
                try:
                    res = calculate_deadline(deadline_key=key, event_date=base_date, rules=rules)
                    if res.status == STATUS_OK and getattr(res, "deadline", None) is not None:
                        due_at = res.deadline.isoformat()
                except Exception:
                    due_at = None

            point = {
                "key": key,
                "norm": entry.get("legal_basis"),
                "base_event": base_event,
                "due_at": due_at,
                "needs_anchor": base_date is None,
                "state": state_val,
            }
            if base_date is None:
                point["warning_basis"] = "event_date_missing"
            points.append(point)
        return instance, points
    finally:
        conn.close()
