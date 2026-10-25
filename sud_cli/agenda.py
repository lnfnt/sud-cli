"""W6a §1 (S-80): единый билдер «calendar agenda» для CLI и MCP.

Логика cmd_calendar_agenda сюда вынесена без изменения формы
конверта (пины v2.5.0 сверяют ключи payload), чтобы MCP-инструмент
sud_cli_calendar_agenda не воспроизводил её второй копией — копия всегда
расходится с оригиналом, поэтому оригинал вызывается напрямую.
"""
from __future__ import annotations

from datetime import date as _date, timedelta
from typing import Optional


def agenda_payload(state_dir, days: int = 14, today: Optional[_date] = None) -> dict:
    from sud_cli.db import get_connection, init_db, list_all_deadline_states, list_cases

    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        states = list_all_deadline_states(conn)
        cases = {c["case_id"]: c for c in list_cases(conn)}
        hearing_rows = conn.execute(
            "SELECT * FROM events WHERE lower(event_type) LIKE '%hearing%' OR lower(event_type) LIKE '%слушани%'"
        ).fetchall()
        hearing_events = [dict(r) for r in hearing_rows]
    finally:
        conn.close()

    today = today or _date.today()
    horizon = today + timedelta(days=days)
    items = []
    for s in states:
        if s.get("state") not in ("computed", "confirmed"):
            continue
        if not s.get("due_at"):
            continue
        case = cases.get(s.get("case_id"))
        if case and case.get("status") == "closed":
            continue
        try:
            due = _date.fromisoformat(str(s["due_at"])[:10])
        except ValueError:
            continue
        items.append({
            "kind": "deadline",
            "case_id": s.get("case_id"),
            "deadline_key": s.get("deadline_key"),
            "due_at": s.get("due_at"),
            "state": s.get("state"),
            "confirmed_by": s.get("confirmed_by"),
            "evidence_note": s.get("evidence_note"),
        })
    for e in hearing_events:
        ed = e.get("event_date")
        if not ed:
            continue
        try:
            due = _date.fromisoformat(str(ed)[:10])
        except ValueError:
            continue
        items.append({
            "kind": "hearing",
            "case_id": e.get("case_id"),
            "event_id": e.get("event_id"),
            "due_at": ed,
            "state": "scheduled",
            "title": e.get("doc_text"),
            "confirmed_by": None,
        })

    def _due(x):
        try:
            return _date.fromisoformat(str(x["due_at"])[:10])
        except Exception:
            return today

    overdue = sorted([i for i in items if _due(i) < today], key=_due)
    upcoming = sorted([i for i in items if today <= _due(i) <= horizon], key=_due)
    next_5 = upcoming[:5]
    return {
        "horizon_days": days,
        "today": today.isoformat(),
        "overdue": overdue,
        "upcoming": upcoming,
        "next_5": next_5,
        "next": next_5,
        "items": sorted(items, key=_due),
        "hearings": [i for i in items if i.get("kind") == "hearing"],
        "empty": len(items) == 0,
    }
