"""W6a §5 (S-23): автозаполнение точек срока из текстов актов (events.doc_text).

Честная модель: маркеры детерминированы (словарь ``autofill_markers.json``),
а юридическая сила у созданной точки — нулевая до подтверждения:
``state=computed``, ``needs_confirm=true``, basis несёт след
``autofill:event:<id>:marker:<name>``. Подтверждённой (confirmed) autofill не
делает никогда — решение за оператором (anti-laches контракт the wave).

Ловушка cure-срока: в утверждённой deadline_matrix строки cure-срока нет,
поэтому маркер «оставлено без движения» НЕ рождает выдуманную точку — он
отдаётся manual_review-hint'ом (словарь ссылается только на существующие
ключи матрицы). Сеть не вызывается; новые keys матрицы не создаются.

Идемпотентность повторного запуска: строки с тем же marker_id не удваиваются
— состояние дела переписывается только когда маркер виден впервые.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

MARKERS_PATH = Path(__file__).with_name("autofill_markers.json")

_WS_RE = re.compile(r"\s+")


def load_markers(path: Optional[Path] = None) -> dict:
    data = json.loads(Path(path or MARKERS_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("markers"), list):
        raise ValueError("autofill dictionary malformed")
    return data


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip().casefold()


def find_markers(doc_text: str, markers: List[dict]) -> List[dict]:
    """Маркеры, чей pattern встретился в тексте (детерминированный порядок словаря)."""
    hay = normalize(doc_text)
    hits = []
    for m in markers:
        for pat in m.get("patterns", []):
            if normalize(pat) in hay:
                hits.append(m)
                break
    return hits


def _marker_id(event_id, marker_name: str) -> str:
    return f"autofill:event:{event_id}:marker:{marker_name}"


def _existing_marker_ids(states) -> set:
    ids = set()
    for s in states:
        basis = s.get("basis")
        if isinstance(basis, dict) and basis.get("marker_id"):
            ids.add(str(basis["marker_id"]))
        elif isinstance(basis, str) and basis.startswith("autofill:"):
            ids.add(basis)
    return ids


def _parse_date(raw) -> Optional[str]:
    if not raw:
        return None
    try:
        datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return str(raw)[:10]


def autofill_case(conn, state_dir, case_id: str, event_id: Optional[str] = None,
                  markers: Optional[dict] = None, rules: Optional[dict] = None) -> dict:
    """Просканровать события дела и создать computed-точки по маркерам.

    Возвращает payload: created / skipped (честный skip-лог) /
    manual_review_hints (cure-маркеры без строки матрицы — точка НЕ создаётся).
    """
    from .deadline_matrix import INSTANCE_TO_JURISDICTION
    from .db import (
        get_case,
        get_case_events,
        insert_deadline_state,
        list_deadline_states,
    )
    from .legal_deadlines import calculate_deadline
    from .legal_engine import STATUS_OK, load_rules

    dictionary = markers or load_markers()
    rules = rules or load_rules()
    matrix = rules.get("deadline_matrix", {}) or {}
    cure_hint = dictionary.get("cure_hint", {})

    case = get_case(conn, case_id)
    if case is None:
        raise ValueError(f"case not found: {case_id}")
    instance = str(case.get("instance") or "").strip().lower()
    target_jurisdiction = INSTANCE_TO_JURISDICTION.get(instance)

    events = get_case_events(conn, case_id)
    if event_id is not None:
        events = [e for e in events if e.get("event_id") == event_id]

    created: List[dict] = []
    skipped: List[dict] = []
    hints: List[dict] = []

    for event in events:
        eid = event.get("event_id")
        hits = find_markers(event.get("doc_text") or "", dictionary["markers"])
        if not hits:
            skipped.append({"event_id": eid, "reason": "no_markers"})
            continue
        event_date = _parse_date(event.get("event_date"))
        if event_date is None:
            skipped.append({"event_id": eid, "reason": "event_date_unparsable",
                            "markers": [m["name"] for m in hits]})
            continue
        for marker in hits:
            name = marker["name"]
            marker_id = _marker_id(eid, name)
            keys = list(marker.get("deadline_keys") or [])
            # v1.4.0 правил (внешняя проверка 2026-10-24): cure-строка есть в матрице —
            # marker создаёт точку (due=null: duration null в строке, дата
            # устранения назначена судом → needs_confirm). Hint остаётся только
            # если у маркера нет ни одного ключа, присутствующего в матрице.
            if not keys or (marker.get("cure") and not any(k in matrix for k in keys)):
                hints.append({
                    "event_id": eid,
                    "marker": name,
                    "code": cure_hint.get("code", "cure_requires_verified_matrix_row"),
                    "hint": cure_hint.get("hint", "cure-срок требует VERIFIED строки матрицы"),
                })
                continue
            if target_jurisdiction:
                keys = [k for k in keys
                        if (matrix.get(k) or {}).get("jurisdiction") == target_jurisdiction]
            for key in keys:
                if key not in matrix:
                    skipped.append({"event_id": eid, "marker": name, "deadline_key": key,
                                    "reason": "key_not_in_matrix"})
                    continue
                states = list_deadline_states(conn, case_id, key)
                if marker_id in _existing_marker_ids(states):
                    skipped.append({"event_id": eid, "marker": name, "deadline_key": key,
                                    "reason": "already_autofilled"})
                    continue
                due_at = None
                try:
                    res = calculate_deadline(deadline_key=key,
                                             event_date=datetime.strptime(event_date, "%Y-%m-%d").date(),
                                             rules=rules)
                    if res.status == STATUS_OK and res.deadline is not None:
                        due_at = res.deadline.isoformat()
                except Exception:
                    due_at = None
                basis = {
                    "source": "autofill",
                    "marker_id": marker_id,
                    "event_id": eid,
                    "marker": name,
                    "event_date": event_date,
                    "method": "autofill",
                    "needs_confirm": True,
                    "actor": "autofill",
                    "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                }
                row = insert_deadline_state(
                    conn, case_id, key, due_at=due_at, basis=basis, state="computed",
                    state_dir=state_dir, actor="autofill", action="autofill",
                    before=states[-1] if states else None,
                )
                out = dict(row)
                out["needs_confirm"] = True
                created.append(out)

    return {
        "case_id": case_id,
        "instance": instance or "unknown",
        "events_scanned": len(events),
        "created": created,
        "skipped": skipped,
        "manual_review_hints": hints,
        "never_confirmed_note": "autofill создаёт только computed/needs_confirm; confirmed — исключительно человек (the wave).",
        "markers_version": dictionary.get("version"),
    }
