"""the wave §2 (S-26): reminders due — bus-free генератор готовых a2a-строк.

Границы:
- sud-cli НИКОГДА не вызывает шину a2a сам: модуль не импортирует ничего,
  что умеет запускать внешние процессы. Здесь только чистые функции над
  списком состояний сроков и сборка строки-текста. Исполняет строку
  оператор или агент-планировщик осознанно, вне sud.
- Источник данных — deadline_states (the wave): state computed|confirmed,
  due_at ISO; due_at NULL и waived пропускаются молча, но не угадываются.
- числа в теле письма есть => по политике a2a строка обязана нести
  --verify "<детерминированная команда>" --verify-run.
"""
from datetime import date, timedelta
from typing import List

DUE_STATES = ("computed", "confirmed")

BUS_FREE_NOTE = (
    "bus-free: sud-cli не вызывает a2a; ниже готовые экранированные строки "
    "команд — исполнение остаётся за оператором"
)

VERIFY_CMD_TEMPLATE = "/usr/bin/sud-cli reminders due --days {days} --json"


def _latest_per_key(states):
    """Последнее состояние на пару (case_id, deadline_key): id растёт с историей."""
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
    return list(latest.values())


def due_items(states, today, days: int) -> List[dict]:
    """Сроки, попадающие в окно [today, today+days], по последнему состоянию."""
    horizon = today + timedelta(days=days)
    items = []
    for s in _latest_per_key(states):
        if s.get("state") not in DUE_STATES:
            continue
        due_raw = s.get("due_at")
        if not due_raw:
            continue
        try:
            due = date.fromisoformat(str(due_raw)[:10])
        except ValueError:
            continue
        if not (today <= due <= horizon):
            continue
        items.append({
            "case_id": s.get("case_id"),
            "deadline_key": s.get("deadline_key"),
            "due_at": due.isoformat(),
            "days_left": (due - today).days,
            "state": s.get("state"),
        })
    items.sort(key=lambda i: (i["due_at"], str(i["case_id"]), str(i["deadline_key"])))
    return items


def a2a_command(item, days: int) -> str:
    """Одна готовая строка a2a mail send; все значения экранированы shlex."""
    import shlex
    subject = f"deadline: {item['case_id']}/{item['deadline_key']} до {item['due_at']}"
    body = (
        f"Напоминание: срок {item['deadline_key']} по делу {item['case_id']} "
        f"до {item['due_at']}, осталось {item['days_left']} дн. "
        f"Окно выборки: {days} дн. Сверка матрицы: sud-cli deadlines list {item['case_id']}. "
        f"Срок может быть просрочен к дате исполнения — проверь до отправки."
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


def generate_mail_commands(items, days: int) -> List[str]:
    return [a2a_command(i, days) for i in items]
