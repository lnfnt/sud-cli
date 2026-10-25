"""the wave §3 (S-27): конфликты расписания — слушания × сроки по разным делам.

Границы:
- набор event_type фиксирован (HEARING_EVENT_TYPES) и сравнивается без
  регистра: «угадывать» по подстроке, что событие является заседанием, движок
  не имеет права — недописанный тип остаётся вне детекта, а не внутри;
- conflict = пересечение НА ОДНУ ДАТУ событий/сроков по РАЗНЫМ делам;
- то же дело, срок + слушание в один день = НЕ конфликт, а подсказка
  (same_case_hints): физически это один и тот же участник, два события в
  один день — напоминание, а не коллизия;
- невалидные даты не валят команду: строка уходит в skipped с причиной;
- списки пустые выводятся явно []; порядок детерминирован.
"""
from __future__ import annotations

from datetime import date as _date, timedelta
from typing import Any, Dict, List, Optional

#: Фиксированный набор типов слушаний (ниже — только точное сравнение lower()).
HEARING_EVENT_TYPES = frozenset({"слушание", "заседание", "предварительное"})

#: Сроки, участвующие в детекте: только вычисленные и подтверждённые.
DEADLINE_STATES = ("computed", "confirmed")

CONFLICTS_DISCLAIMER = (
    "Детект справочный: он видит только слушания с event_type из фиксированного "
    "набора {слушание, заседание, предварительное} и сроки computed|confirmed "
    "локального реестра. Отсутствие пересечений не означает, что их нет: "
    "командировки, несколько процессов у одного лица и время на дорогу не "
    "покрыты данными. Решение — за человеком."
)


def _parse_date(raw: Any) -> Optional[_date]:
    if raw is None or str(raw) == "":
        return None
    try:
        return _date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _latest_per_key(states: List[dict]) -> List[dict]:
    """Последнее состояние на пару (case_id, deadline_key).

    Не вторая копия логики: тот же хелпер, что у reminders (the wave) — копия
    окна/истории расходится с оригиналом при первой же правке.
    """
    from .reminders import _latest_per_key as latest

    return latest(states)


def collect_entries(conn, window_from: _date, window_to: _date):
    """Записи расписания в окне: слушания (фикс. набор типов) + сроки.

    Возвращает (entries, skipped). entry = {kind, case_id, ref, on_date, label}.
    """
    entries: List[dict] = []
    skipped: List[dict] = []

    for row in conn.execute("SELECT * FROM events ORDER BY event_id ASC").fetchall():
        ev = dict(row)
        etype = ("" if ev.get("event_type") is None else str(ev["event_type"]).strip().lower())
        if etype not in HEARING_EVENT_TYPES:
            continue
        day = _parse_date(ev.get("event_date"))
        if day is None:
            skipped.append({
                "kind": "hearing",
                "case_id": ev.get("case_id"),
                "ref": ev.get("event_id"),
                "reason": f"нечитаемая дата события: {ev.get('event_date')!r}",
            })
            continue
        if not (window_from <= day <= window_to):
            continue
        entries.append({
            "kind": "hearing",
            "case_id": ev.get("case_id"),
            "ref": ev.get("event_id"),
            "event_type": ev.get("event_type"),
            "on_date": day.isoformat(),
            "label": ev.get("doc_text"),
        })

    from .db import list_all_deadline_states

    for s in _latest_per_key(list_all_deadline_states(conn)):
        if s.get("state") not in DEADLINE_STATES:
            continue
        day = _parse_date(s.get("due_at"))
        if day is None:
            if s.get("due_at"):
                skipped.append({
                    "kind": "deadline",
                    "case_id": s.get("case_id"),
                    "ref": s.get("deadline_key"),
                    "reason": f"нечитаемая due_at: {s.get('due_at')!r}",
                })
            continue
        if not (window_from <= day <= window_to):
            continue
        entries.append({
            "kind": "deadline",
            "case_id": s.get("case_id"),
            "ref": s.get("deadline_key"),
            "state": s.get("state"),
            "on_date": day.isoformat(),
            "label": s.get("deadline_key"),
        })

    entries.sort(key=lambda e: (e["on_date"], str(e["case_id"]), e["kind"], str(e["ref"])))
    skipped.sort(key=lambda e: (str(e["case_id"]), str(e["ref"])))
    return entries, skipped


def pair_conflicts(entries: List[dict]) -> List[dict]:
    """Конфликты: одна дата, разные дела. Пары дел — без дублей и без повторов."""
    by_day: Dict[str, List[dict]] = {}
    for e in entries:
        by_day.setdefault(e["on_date"], []).append(e)
    conflicts: List[dict] = []
    for day in sorted(by_day):
        items = by_day[day]
        pairs: Dict[tuple, dict] = {}
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a["case_id"] == b["case_id"]:
                    continue
                case_a, case_b = sorted([str(a["case_id"]), str(b["case_id"])])
                pk = (day, case_a, case_b)
                rec = pairs.setdefault(pk, {
                    "date": day,
                    "case_a": case_a,
                    "case_b": case_b,
                    "entries": [],
                })
                for e in (a, b):
                    token = (e["kind"], str(e["case_id"]), str(e["ref"]))
                    if token not in [tuple(x.get("_k", [])) for x in rec["entries"]]:
                        rec["entries"].append({
                            "_k": list(token),
                            "case_id": e["case_id"],
                            "kind": e["kind"],
                            "ref": e["ref"],
                            "detail": e.get("event_type") or e.get("state"),
                        })
        for rec in sorted(pairs.values(), key=lambda r: (r["date"], r["case_a"], r["case_b"])):
            conflicts.append({
                "date": rec["date"],
                "case_a": rec["case_a"],
                "case_b": rec["case_b"],
                "kinds": sorted({e["kind"] for e in rec["entries"]}),
                "entries": [
                    {k: v for k, v in e.items() if k != "_k"} for e in rec["entries"]
                ],
            })
    return conflicts


def same_case_hints(entries: List[dict]) -> List[dict]:
    """Подсказки: тот же день, то же дело, срок + слушание (не конфликт)."""
    by_day_case: Dict[tuple, List[dict]] = {}
    for e in entries:
        by_day_case.setdefault((e["on_date"], str(e["case_id"])), []).append(e)
    hints = []
    for (day, case_id) in sorted(by_day_case):
        items = by_day_case[(day, case_id)]
        kinds = {i["kind"] for i in items}
        if "hearing" in kinds and "deadline" in kinds:
            hints.append({
                "date": day,
                "case_id": case_id,
                "hearings": [i["ref"] for i in items if i["kind"] == "hearing"],
                "deadlines": [i["ref"] for i in items if i["kind"] == "deadline"],
                "note": "срок и слушание одного дела в один день — это "
                        "подсказка, а не конфликт разных дел",
            })
    return hints


def conflicts_payload(state_dir: Any, days: int = 30,
                      today: Optional[_date] = None) -> Dict[str, Any]:
    from .db import get_connection, init_db

    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        today = today or _date.today()
        horizon = today + timedelta(days=days)
        entries, skipped = collect_entries(conn, today, horizon)
    finally:
        conn.close()
    return {
        "days": days,
        "today": today.isoformat(),
        "window_to": horizon.isoformat(),
        "hearing_event_types": sorted(HEARING_EVENT_TYPES),
        "entries": entries,
        "conflicts": pair_conflicts(entries),
        "same_case_hints": same_case_hints(entries),
        "skipped": skipped,
        "empty": not entries,
        "disclaimer": CONFLICTS_DISCLAIMER,
    }
