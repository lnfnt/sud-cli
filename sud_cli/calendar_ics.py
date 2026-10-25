"""W6a §3 (S-28): экспорт дедлайнов в RFC 5545 (ICS).

Источник — deadline_states computed/confirmed (the wave), по последнему состоянию
на пару (case_id, deadline_key); waived и due_at=NULL не экспортируются.
Конверт детерминирован: DTSTAMP выводится из due-даты, поэтому повторный
экспорт тех же состояний даёт побайтово тот же файл (UID стабильны —
требование идемпотентности из ТЗ).

Формат (RFC 5545):
- CRLF переносы; строки не длиннее 75 октетов — длинные фолдятся continuation
  пробелом, разбивка идёт по границам символов utf-8;
- спецсимволы TEXT-значений экранируются: ``\\\\`` -> ``\\\\\\\\``, ``;`` -> ``\\\\;``,
  ``,`` -> ``\\\\,``, перевод строки -> ``\\\\n``;
- DTSTART/DTEND — локальное время суда (Europe/Moscow, 09:00, +1 час) с явным
  TZID и блоком VTIMEZONE: календарь без таймзоны агент прочтёт как UTC;
- VALARM -P1D ставится ТОЛЬКО на confirmed: напоминать за сутки по unverified
  вычисленному сроку — давление на оператора, а не факт (anti-laches).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Tuple

PRODID = "-//sud-cli//DEADLINES RU 2.11.0//EN"
TZID = "Europe/Moscow"
DUE_HOUR = 9  # процессуальные сроки истекают в конце дня; событие ставим на утро суда
LINE_LEN = 75  # октетов, RFC 5545 3.1
EXPORT_STATES = ("computed", "confirmed")

_VTIMEZONE = (
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:STANDARD",
    "DTSTART:20141026T000000",
    "TZOFFSETFROM:+0400",
    "TZOFFSETTO:+0300",
    "TZNAME:MSK",
    "END:STANDARD",
    "END:VTIMEZONE",
)


def _esc(text) -> str:
    out = str(text if text is not None else "")
    out = out.replace("\\", "\\\\")
    out = out.replace(";", "\\;")
    out = out.replace(",", "\\,")
    out = out.replace("\r\n", "\\n").replace("\n", "\\n")
    return out


def _fold(line: str) -> list[str]:
    """Разбить строку на куски <= 75 октетов; continuation-строки с ведущим
    пробелом (RFC 5545 3.1), разбивка по границам символов utf-8."""
    if len(line.encode("utf-8")) <= LINE_LEN:
        return [line]
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    limit = LINE_LEN
    for ch in line:
        ch_len = len(ch.encode("utf-8"))
        if current_len + ch_len > limit:
            pieces.append("".join(current))
            current = [ch]
            current_len = ch_len
            limit = LINE_LEN - 1  # continuation-строка начинается с пробела
        else:
            current.append(ch)
            current_len += ch_len
    if current:
        pieces.append("".join(current))
    return [pieces[0]] + [" " + p for p in pieces[1:]]


def _line(name: str, value: str) -> list[str]:
    return _fold(f"{name}:{value}")


def event_uid(case_id: str, deadline_key: str) -> str:
    """Стабильный UID: пара (дело, ключ) — не id строки истории."""
    return f"{case_id}-{deadline_key}@sud-cli"


def _latest_per_key(states):
    latest = {}
    for s in states:
        key = (s.get("case_id"), s.get("deadline_key"))
        try:
            sid = int(s.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        prev = latest.get(key)
        if prev is None or sid >= int(prev.get("id") or 0):
            latest[key] = s
    return latest


def _describe(state: dict) -> str:
    basis = state.get("basis")
    if isinstance(basis, dict):
        basis_txt = "; ".join(f"{k}={v}" for k, v in sorted(basis.items()))
    elif basis:
        basis_txt = str(basis)
    else:
        basis_txt = "-"
    parts = [f"case={state.get('case_id')}", f"key={state.get('deadline_key')}",
             f"state={state.get('state')}", f"basis: {basis_txt}"]
    if state.get("evidence_note"):
        parts.append(f"evidence: {state['evidence_note']}")
    if state.get("confirmed_by"):
        parts.append(f"confirmed_by: {state['confirmed_by']}")
    return " | ".join(parts)


def build_ics(states, today: Optional[date] = None, days: int = 60) -> Tuple[str, list[dict]]:
    """VCALENDAR-текст (CRLF) + список событий; пустой вход = валидный пустой календарь."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    events_meta: list[dict] = []
    body: list[str] = []
    chosen = []
    for (case_id, key), s in sorted(_latest_per_key(states).items(),
                                    key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        if s.get("state") not in EXPORT_STATES:
            continue
        raw_due = s.get("due_at")
        if not raw_due:
            continue
        try:
            due = date.fromisoformat(str(raw_due)[:10])
        except ValueError:
            continue
        if not (today <= due <= horizon):
            continue
        chosen.append((case_id, key, s, due))

    for case_id, key, s, due in chosen:
        uid = event_uid(str(case_id), str(key))
        stamp = f"{due.strftime('%Y%m%d')}T000000Z"
        start = f"{due.strftime('%Y%m%d')}T{DUE_HOUR:02d}0000"
        # DTEND = DTSTART + 1 час; дата та же, поскольку DUE_HOUR+1 < 24.
        end = f"{due.strftime('%Y%m%d')}T{DUE_HOUR + 1:02d}0000"
        lines: list[str] = ["BEGIN:VEVENT"]
        lines += _line("UID", _esc(uid))
        lines += _line("DTSTAMP", stamp)
        lines += _line("DTSTART;TZID=" + TZID, start)
        lines += _line("DTEND;TZID=" + TZID, end)
        lines += _line("SUMMARY", _esc(f"deadline: {key} {case_id}"))
        lines += _line("DESCRIPTION", _esc(_describe(s)))
        lines += _line("STATUS", "CONFIRMED")
        if s.get("state") == "confirmed":
            lines += ["BEGIN:VALARM"]
            lines += _line("ACTION", "DISPLAY")
            lines += _line("TRIGGER", "-P1D")
            lines += _line("DESCRIPTION", _esc(f"deadline reminder: {key} {case_id}"))
            lines += ["END:VALARM"]
        lines.append("END:VEVENT")
        body += lines
        events_meta.append({
            "uid": uid,
            "case_id": case_id,
            "deadline_key": key,
            "due_at": due.isoformat(),
            "state": s.get("state"),
            "alarm": s.get("state") == "confirmed",
        })

    out: list[str] = ["BEGIN:VCALENDAR"]
    out += _line("VERSION", "2.0")
    out += _line("PRODID", PRODID)
    out += _line("CALSCALE", "GREGORIAN")
    out += _line("METHOD", "PUBLISH")
    out += list(_VTIMEZONE)
    out += body
    out.append("END:VCALENDAR")
    text = "\r\n".join(out) + "\r\n"
    return text, events_meta


def ics_for_state_dir(state_dir, days: int = 60,
                      today: Optional[date] = None) -> Tuple[str, list[dict]]:
    from sud_cli.db import get_connection, init_db, list_all_deadline_states

    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        states = list_all_deadline_states(conn)
    finally:
        conn.close()
    return build_ics(states, today=today, days=days)
