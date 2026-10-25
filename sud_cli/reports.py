"""the wave §4 (S-84): отчёты портфеля и дела — только счётчики из реальных таблиц.

Правило «не выдуманных метрик»: здесь нет ни «успешности», ни «рисков», ни
«средних» — только то, что читается из cases/events/amounts/deadline_states/
evidence_items/case_representatives. Горизонт near/overdue переиспользует
логику agenda (W6a) — вторая копия окна не заводится, копии всегда расходятся.
Пустые секции выводятся явно [] / {}, а не «нет данных».
"""
from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .db import (
    LEGAL_CASE_STATUSES,
    get_case,
    get_case_card,
    get_connection,
    init_db,
    list_cases,
    summarize_case_amounts,
    representatives_card_section,
    verify_case_evidence,
    evidence_summary,
)

NEAR_HORIZON_DAYS = 30

REPORT_DISCLAIMER = (
    "Отчёт справочный: считает только строки локального реестра sud-cli. "
    "Статусы дел — внесённые оператором, не факт суда; суммы — внесённые "
    "расходы; сроки — computed|confirmed из deadline_states. Метрик успеха, "
    "риска или «качества ведения» здесь нет и быть не может: sud-cli их не "
    "наблюдает."
)


def _zero() -> Decimal:
    return Decimal("0")


def _near_overdue(state_dir: Any, today: _date, days: int):
    """Сроки в окне [today, today+days] и просроченные — из agenda-билдера (W6a).

    Окно не переопределяется второй копией: near/overdue берутся из
    agenda_payload — там же живёт правило «закрытое дело не плодит пунктов»
    и то же определение computed|confirmed. Здесь только суждение до
    kind=deadline и подстановка days_left.
    """
    from .agenda import agenda_payload

    payload = agenda_payload(state_dir, days=days, today=today)

    def _pick(items):
        out = []
        for item in items:
            if item.get("kind") != "deadline":
                continue
            out.append({
                "case_id": item.get("case_id"),
                "deadline_key": item.get("deadline_key"),
                "due_at": item.get("due_at"),
                "state": item.get("state"),
                "days_left": (_date.fromisoformat(str(item["due_at"])[:10])
                              - today).days,
            })
        return out

    return _pick(payload["upcoming"]), _pick(payload["overdue"]), payload


def _closed_dropped_deadlines(conn) -> List[dict]:
    """Сроки закрытых дел, которые agenda-правило не показывает.

    Переиспользование окна (W6a) означает, что у закрытого дела его computed/
    confirmed сроки в near/overdue не попадают. Это не повод терять их молча:
    они считаются здесь и выводятся отдельным списком с причиной, чтобы
    приёмщик видел, что именно скрыло правило, а не «сроков нет».
    """
    from .db import list_all_deadline_states
    from .reminders import _latest_per_key

    closed = {c["case_id"] for c in list_cases(conn) if c.get("status") == "closed"}
    dropped = []
    for s in _latest_per_key(list_all_deadline_states(conn)):
        if s.get("case_id") not in closed:
            continue
        if s.get("state") not in ("computed", "confirmed") or not s.get("due_at"):
            continue
        dropped.append({
            "case_id": s.get("case_id"),
            "deadline_key": s.get("deadline_key"),
            "due_at": s.get("due_at"),
            "state": s.get("state"),
            "reason": "agenda-правило: закрытое дело не участвует в near/overdue",
        })
    dropped.sort(key=lambda i: (str(i["due_at"]), str(i["case_id"]),
                                str(i["deadline_key"])))
    return dropped


def portfolio_payload(state_dir: Any, as_of: Optional[str] = None,
                      days: int = NEAR_HORIZON_DAYS) -> Dict[str, Any]:
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        cases = list_cases(conn)
        today = _date.today()
        if as_of:
            today = _date.fromisoformat(str(as_of)[:10])

        by_status: Dict[str, int] = {}
        for c in cases:
            key = c.get("status") or "unknown"
            by_status[key] = by_status.get(key, 0) + 1
        # статусы enum'а всегда присутствуют в выводе: пустой портфель даёт
        # нули по всем LEGAL_CASE_STATUSES, а не «секции нет».
        statuses = {s: int(by_status.get(s, 0)) for s in LEGAL_CASE_STATUSES}
        for s, n in by_status.items():      # значения вне enum — честно видно
            if s not in statuses:
                statuses[s] = n

        spent = _zero()
        planned = _zero()
        refunded = _zero()
        by_kind: Dict[str, str] = {}
        amounts_cases = 0
        for c in cases:
            summary = summarize_case_amounts(conn, c["case_id"])
            rows = summary["rows"]
            if rows:
                amounts_cases += 1
            for r in rows:
                dec = Decimal(str(r["total"]))
                by_kind[r["kind"]] = str(Decimal(by_kind.get(r["kind"], "0")) + dec)
                status = r["status"] or "planned"
                if status == "paid":
                    spent += dec
                elif status == "refunded":
                    refunded += dec
                else:
                    planned += dec

        reps_total = 0
        reps_by_side: Dict[str, int] = {}
        for c in cases:
            section = representatives_card_section(conn, c["case_id"])
            reps_total += section["count"]
            for side, n in section["by_side"].items():
                reps_by_side[side] = reps_by_side.get(side, 0) + n

        evidence_total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_items").fetchone()["n"])
        evidence_problems = int(conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_items WHERE sha256 IS NULL").fetchone()["n"])
        documents_total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM case_documents").fetchone()["n"])
        documents_revisions = int(conn.execute(
            "SELECT COUNT(*) AS n FROM case_documents WHERE supersedes IS NOT NULL"
        ).fetchone()["n"])
        events_total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM events").fetchone()["n"])

        near, overdue, _agenda = _near_overdue(state_dir, today, days)
        closed_dropped = _closed_dropped_deadlines(conn)
        # problems — только счётчики из таблиц состояния (ТЗ §4: без verify,
        # который пересчитывает файлы: это уже не «counts из state tables»).
        problems = [
            {
                "case_id": c["case_id"],
                "status": c.get("status") or "unknown",
                "events": int(conn.execute(
                    "SELECT COUNT(*) AS n FROM events WHERE case_id = ?",
                    (c["case_id"],)).fetchone()["n"]),
                "deadline_states_without_due": int(conn.execute(
                    "SELECT COUNT(*) AS n FROM deadline_states "
                    "WHERE case_id = ? AND due_at IS NULL",
                    (c["case_id"],)).fetchone()["n"]),
            }
            for c in cases
        ]
    finally:
        conn.close()

    return {
        "as_of": today.isoformat(),
        "horizon_days": days,
        "cases": {
            "total": len(cases),
            "with_amounts": amounts_cases,
            "by_status": statuses,
        },
        "events": {"total": events_total},
        "deadlines": {
            "near": near,
            "near_count": len(near),
            "overdue": overdue,
            "overdue_count": len(overdue),
            # окно — agenda-правило (W6a): у закрытого дела сроки в near/overdue
            # не попадают; они перечислены здесь, а не потеряны молча.
            "closed_excluded": closed_dropped,
            "closed_excluded_count": len(closed_dropped),
        },
        "amounts": {
            "cases_with_amounts": amounts_cases,
            "by_kind": by_kind,
            "totals": {
                "paid": str(spent),
                "planned": str(planned),
                "refunded": str(refunded),
            },
            "currency": "RUB",
        },
        "representatives": {
            "total": reps_total,
            "by_side": {s: int(reps_by_side.get(s, 0)) for s in ("plaintiff", "defendant")},
        },
        "documents": {"total": documents_total, "revisions": documents_revisions},
        "evidence": {"total": evidence_total, "without_hash": evidence_problems},
        "problems": problems,
        "portfolio": [
            {
                "case_id": c["case_id"],
                "case_number": c.get("case_number"),
                "court": c.get("court"),
                "status": c.get("status"),
                "instance": c.get("instance"),
                "owner": c.get("owner"),
            }
            for c in cases
        ],
        "disclaimer": REPORT_DISCLAIMER,
    }


def case_payload(state_dir: Any, case_id: str) -> Optional[Dict[str, Any]]:
    """Печатная развёртка дела: секции card + матрица сроков + evidence-итог."""
    from .deadline_matrix import build_matrix

    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        if get_case(conn, case_id) is None:
            return None
        card = get_case_card(conn, case_id)
        amounts = summarize_case_amounts(conn, case_id)
        items = verify_case_evidence(conn, case_id)
        summary = evidence_summary(items)
    finally:
        conn.close()

    instance, points = build_matrix(state_dir, case_id)
    matrix_summary = {
        "instance": instance,
        "points_total": len(points),
        "anchored": [p["key"] for p in points if not p.get("needs_anchor")],
        "needs_anchor": [p["key"] for p in points if p.get("needs_anchor")],
        "computed": [p["key"] for p in points if p.get("due_at")],
    }

    sections = {
        "case": card.get("case"),
        "parties": card.get("parties"),
        "amounts": card.get("amounts"),
        "documents": card.get("documents"),
        "documents_current": card.get("documents_current"),
        "notes": card.get("notes"),
        "events": card.get("events"),
        "deadlines": card.get("deadlines"),
        "evidence": card.get("evidence"),
        "representatives": card.get("representatives"),
    }
    return {
        "case_id": case_id,
        "sections": sections,
        "amounts_summary": amounts,
        "evidence_verify": {k: v for k, v in summary.items() if k != "problem_items"},
        "evidence_problems": summary["problem_items"],
        "matrix": matrix_summary,
        "disclaimer": REPORT_DISCLAIMER,
    }


def render_portfolio_text(payload: Dict[str, Any]) -> List[str]:
    """Human-рендер портфеля: строки по фактическим секциям, без «красивых» выводов."""
    lines = [
        f"Портфель на {payload['as_of']} (горизонт {payload['horizon_days']} дн.)",
        f"дел: {payload['cases']['total']}",
    ]
    lines.append("по статусам:")
    for status, count in sorted(payload["cases"]["by_status"].items()):
        lines.append(f"  {status}: {count}")
    lines.append(f"сроки near (<= {payload['horizon_days']} дн.): "
                 f"{payload['deadlines']['near_count']}")
    for item in payload["deadlines"]["near"]:
        lines.append(f"  {item['due_at']} {item['case_id']} {item['deadline_key']} "
                     f"{item['state']} ({item['days_left']} дн.)")
    lines.append(f"просрочено: {payload['deadlines']['overdue_count']}")
    for item in payload["deadlines"]["overdue"]:
        lines.append(f"  {item['due_at']} {item['case_id']} {item['deadline_key']} "
                     f"{item['state']}")
    dropped = payload["deadlines"].get("closed_excluded") or []
    lines.append(f"вне окна (закрытые дела, правило agenda): {len(dropped)}")
    for item in dropped:
        lines.append(f"  {item['due_at']} {item['case_id']} {item['deadline_key']} "
                     f"{item['state']}")
    totals = payload["amounts"]["totals"]
    lines.append(f"расходы: paid={totals['paid']} planned={totals['planned']} "
                 f"refunded={totals['refunded']} RUB")
    lines.append(f"представители: {payload['representatives']['total']} "
                 f"(plaintiff={payload['representatives']['by_side']['plaintiff']}, "
                 f"defendant={payload['representatives']['by_side']['defendant']})")
    lines.append(f"документы: {payload['documents']['total']} "
                 f"(ревизий: {payload['documents']['revisions']})")
    lines.append(f"доказательства: {payload['evidence']['total']} "
                 f"(без hash: {payload['evidence']['without_hash']})")
    lines.append(f"problems (по делам): {len(payload['problems'])}")
    for p in payload["problems"]:
        if p["deadline_states_without_due"] or p["events"]:
            lines.append(
                f"  {p['case_id']}: status={p['status']} events={p['events']} "
                f"сроков без due_at={p['deadline_states_without_due']}"
            )
    lines.append(payload["disclaimer"])
    return lines


def render_case_text(payload: Dict[str, Any]) -> List[str]:
    case = payload["sections"].get("case") or {}
    lines = [
        f"Дело {payload['case_id']}: {case.get('case_number')} ({case.get('court')})",
        f"статус: {case.get('status')} | instance: {case.get('instance')} "
        f"| owner: {case.get('owner')}",
        f"стороны: истец {case.get('plaintiff')} / ответчик {case.get('defendant')}",
        f"событий: {len(payload['sections'].get('events') or [])}",
        f"заметок: {len(payload['sections'].get('notes') or [])}",
        f"документов: {len(payload['sections'].get('documents') or [])} "
        f"(текущих: {len(payload['sections'].get('documents_current') or [])})",
        f"доказательств: {payload['sections']['evidence']['count']} "
        f"(проблемных: {len(payload['evidence_problems'])})",
        f"представителей: {payload['sections']['representatives']['count']}",
        f"расходы: {payload['amounts_summary']['totals_by_status']}",
        f"матрица: instance={payload['matrix']['instance']} "
        f"точек={payload['matrix']['points_total']} "
        f"нужен якорь={len(payload['matrix']['needs_anchor'])}",
        payload["disclaimer"],
    ]
    return lines
