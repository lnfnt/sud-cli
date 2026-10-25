"""W6a §4 (S-60): локальные алерты case-search → bus-free a2a-строки.

Эталон паттерна — reminders.py (the wave): sud-cli НИКОГДА не вызывает шину a2a
сам и вообще не запускает внешние процессы. Модуль читает artifacts
case_search (alert-файлы и dedup-store), фильтрует окно по created_at и
печатает готовые shlex-экранированные строки `a2a mail send ...`. Исполняет
строку оператор или агент-планировщик, вне sud.

Числа в теле письма есть => строка несёт --verify "<детерминированная
команда>" --verify-run (политика a2a, как в the wave).

Пустой список — пустой вывод ([]), а не ошибка: отсутствие новостей по суду
не повод падать.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import List

BUS_FREE_NOTE = (
    "bus-free: sud-cli не вызывает a2a; ниже готовые экранированные строки "
    "команд — исполнение остаётся за оператором"
)

VERIFY_CMD_TEMPLATE = "/usr/bin/sud-cli case-search alerts --days {days} --json"

#: dedup-store лежит в каталоге алертов и не является alert-файлом.
SEEN_STORE_NAME = "seen_fingerprints.json"


def alerts_search_dirs(state_dir) -> List[Path]:
    """Кандидатские каталоги алертов: sud-state и стандартный cwd-root cw."""
    dirs = [Path(state_dir) / "case-search-alerts"]
    cw_root = Path.cwd() / ".case-search" / "alerts"
    if cw_root not in dirs:
        dirs.append(cw_root)
    return dirs


def _alert_files(alerts_dir: Path) -> List[Path]:
    if not alerts_dir.is_dir():
        return []
    return sorted(p for p in alerts_dir.glob("*.json") if p.name != SEEN_STORE_NAME)


def load_alert_files(alerts_dir) -> List[dict]:
    """Прочитать alert-файлы; битые/чужие JSON молча пропускаются (не угадываем)."""
    out: List[dict] = []
    for path in _alert_files(Path(alerts_dir)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or "case_number" not in data or "created_at" not in data:
            continue
        out.append(data)
    return out


def recent_alerts(alerts: List[dict], today: date, days: int) -> List[dict]:
    """Алерты, созданные в окне [today-days, today]; детерминированный порядок."""
    floor_day = today - timedelta(days=days)
    kept = []
    for a in alerts:
        raw = str(a.get("created_at") or "")
        try:
            created = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if floor_day <= created <= today:
            kept.append(a)
    kept.sort(key=lambda a: (str(a.get("created_at") or ""), str(a.get("case_number")),
                             str(a.get("status"))))
    return kept


def a2a_command(alert: dict, days: int) -> str:
    """Одна готовая строка a2a mail send; все значения экранированы shlex."""
    import shlex
    case_number = alert.get("case_number")
    status = alert.get("status")
    reason = alert.get("reason")
    changes = alert.get("changes") or []
    paths = "; ".join(str(c.get("path")) for c in changes[:5] if isinstance(c, dict))
    subject = f"case-search: {case_number} — {status}"
    body = (
        f"Локальный алерт case-search: дело {case_number}, статус {status}, "
        f"причина: {reason}; изменений: {len(changes)}. "
        + (f"Пути: {paths}. " if paths else "")
        + f"Окно выборки: {days} дн. Повторная сверка: {VERIFY_CMD_TEMPLATE.format(days=days)} "
          "Алерт может устареть к моменту отправки — проверь артефакты до исполнения строки."
    )
    verify = VERIFY_CMD_TEMPLATE.format(days=days)
    parts = [
        "a2a", "mail", "send",
        "--to", "colleague-legal",
        "--priority", "today",
        "--subject", subject,
        "--body", body,
        "--verify", verify,
        "--verify-run",
    ]
    return " ".join(shlex.quote(p) for p in parts)


def generate_mail_commands(alerts: List[dict], days: int) -> List[str]:
    return [a2a_command(a, days) for a in alerts]


def alerts_payload(alerts_dir, today: date, days: int) -> dict:
    """Собрать готовый payload команды (используется CLI и MCP одним кодом)."""
    raw = load_alert_files(alerts_dir)
    recent = recent_alerts(raw, today, days)
    return {
        "days": days,
        "today": today.isoformat(),
        "source_dir": str(alerts_dir),
        "total_files": len(raw),
        "alerts": recent,
        "commands": generate_mail_commands(recent, days),
        "bus_free": BUS_FREE_NOTE,
    }


def count_pending_alerts(state_dir) -> int:
    """Сколько алертов ещё не переданы в шину (fingerprint нет в dedup-store).

    Только чтение локальных файлов; шина не трогается. Для doctor-счётчика.
    """
    from .case_search.alerts import Alert
    from .case_search.alerts_dedup import (
        alert_fingerprint,
        default_seen_path,
        load_seen_fingerprints,
    )

    seen = set(load_seen_fingerprints(default_seen_path()))
    pending = 0
    for alerts_dir in alerts_search_dirs(state_dir):
        store = alerts_dir / SEEN_STORE_NAME
        if store != default_seen_path():
            seen |= set(load_seen_fingerprints(store))
        for raw in load_alert_files(alerts_dir):
            alert = Alert(
                case_number=str(raw.get("case_number")),
                status=str(raw.get("status") or ""),
                reason=str(raw.get("reason") or ""),
                changes=list(raw.get("changes") or []),
                destination=str(raw.get("destination") or "local-file"),
                created_at=str(raw.get("created_at") or ""),
            )
            if alert_fingerprint(alert.case_number, alert) not in seen:
                pending += 1
    return pending
