"""история §5 (S-86): метрики state-dir — счётчики из таблиц, без выдуманных KPI.

Границы:
- ни одного «индекса здоровья», «риска» или «прогресса»: поле за полем это
  COUNT(*)/GROUP BY по реальным таблицам sud-cli;
- письма/журнал не трогаем — это шина a2a, не данные sud (bus-free);
- last backup берётся из ``meta``: путь пишет сам ``backup create``, и ничего
  не вынюхивается по диску;
- пустая таблица даёт 0, а отсутствующая секция — []/{}: числа выводятся
  только из запросов, «среднего» не выдумываем.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, Optional

from .db import (
    LEGAL_CASE_STATUSES,
    get_connection,
    init_db,
    list_all_deadline_states,
    list_cases,
)

#: Окна совпадают с agenda/reminders: своё «среднее» окно здесь не изобретается.
NEAR_DAYS = 30
REMINDERS_DAYS = 7

METRICS_DISCLAIMER = (
    "Метрики — счётчики строк локального реестра (state tables). Это не KPI "
    "и не оценка качества работы: sud-cli наблюдает только внесённые данные."
)

_TABLES_WITH_CASE = (
    "events",
    "case_amounts",
    "case_documents",
    "case_notes",
    "deadline_states",
    "evidence_items",
    "case_representatives",
)


def _counts_by_status(cases) -> Dict[str, int]:
    """Все статусы enum'а всегда на месте: пустой портфель даёт нули, не {}."""
    out = {s: 0 for s in LEGAL_CASE_STATUSES}
    for c in cases:
        key = c.get("status") if c.get("status") is not None else "unknown"
        out[key] = out.get(key, 0) + 1
    return out


def metrics_payload(state_dir: Any, today: Optional[_date] = None,
                    near_days: int = NEAR_DAYS) -> Dict[str, Any]:
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        today = today or _date.today()
        cases = list_cases(conn)
        states = list_all_deadline_states(conn)

        states_by_state: Dict[str, int] = {}
        for s in states:
            key = str(s.get("state"))
            states_by_state[key] = states_by_state.get(key, 0) + 1

        from .reminders import due_items
        from .reports import _near_overdue

        reminders_pending = len(due_items(states, today, REMINDERS_DAYS))
        near, over, _agenda = _near_overdue(state_dir, today, near_days)

        rows_per_table: Dict[str, int] = {}
        for table in _TABLES_WITH_CASE:
            rows_per_table[table] = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )

        docs_revisions = int(conn.execute(
            "SELECT COUNT(*) AS n FROM case_documents WHERE supersedes IS NOT NULL"
        ).fetchone()["n"])
        evidence_without_hash = int(conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_items WHERE sha256 IS NULL"
        ).fetchone()["n"])
        reps_by_side = {
            str(row["side"]): int(row["n"])
            for row in conn.execute(
                "SELECT side, COUNT(*) AS n FROM case_representatives "
                "GROUP BY side ORDER BY side ASC"
            ).fetchall()
        }

        from .backup import last_backup

        last = last_backup(state_dir)
    finally:
        conn.close()

    return {
        "as_of": today.isoformat(),
        "cases": {"total": len(cases), "by_status": _counts_by_status(cases)},
        "events": rows_per_table["events"],
        "deadline_states": {"total": len(states), "by_state": states_by_state},
        "deadlines_window": {
            "near_days": near_days,
            "near": len(near),
            "overdue": len(over),
        },
        "documents": {"total": rows_per_table["case_documents"],
                      "revisions": docs_revisions},
        "evidence": {"total": rows_per_table["evidence_items"],
                     "without_hash": evidence_without_hash},
        "representatives": {"total": rows_per_table["case_representatives"],
                            "by_side": reps_by_side},
        "notes": rows_per_table["case_notes"],
        "amounts": rows_per_table["case_amounts"],
        "reminders": {"pending_days": REMINDERS_DAYS, "pending": reminders_pending},
        "rows_per_table": rows_per_table,
        "backup": {
            "last": last,
            "recorded_in": "meta",
        },
        "disclaimer": METRICS_DISCLAIMER,
    }


def doctor_metrics(state_dir: Any) -> Dict[str, Any]:
    """Сжатая секция для doctor: те же счётчики, без вложенных окон."""
    payload = metrics_payload(state_dir)
    return {
        "cases": payload["cases"],
        "events": payload["events"],
        "deadline_states": payload["deadline_states"],
        "deadlines_window": payload["deadlines_window"],
        "documents": payload["documents"],
        "evidence": payload["evidence"],
        "representatives": payload["representatives"],
        "reminders": payload["reminders"],
        "backup": payload["backup"],
        "disclaimer": payload["disclaimer"],
    }
