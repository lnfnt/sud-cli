"""W6a §1 (S-80): реестр идемпотентности write-инструментов MCP.

Повтор write-вызова с тем же ``idempotency_key`` в окне 10 минут обязан
вернуть ПЕРВЫЙ результат, а не устроить вторую запись. Реестр живёт в
state-dir (``idempotency.json``, права 0600 — в значении могут быть реквизиты
дела) и чистится по тому же окну: запись старше 600 с больше ничего не
подавляет.

Границы: модуль не знает про сеть и про MCP-транспорт — только чтение/запись
одного файла. Повреждённый реестр трактуется как пустой (fail-open для
легальных записей): потерянное подавление повтора видимо по audit-журналу, а
заблокировать законный вызов молча — худший из исходов.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

WINDOW_SECONDS = 600  # окно подавления повторов: 10 минут = первый результат
REGISTRY_NAME = "idempotency.json"


def registry_path(state_dir) -> Path:
    return Path(state_dir) / REGISTRY_NAME


def _load(state_dir) -> dict:
    path = registry_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_id(tool: str, key: str) -> str:
    return f"{tool}|{key}"


def lookup(state_dir, tool: str, key: str, now: float | None = None):
    """Первый результат по (tool, key), если он ещё в окне; иначе None."""
    now = time.time() if now is None else now
    entry = _load(state_dir).get(_entry_id(tool, key))
    if not isinstance(entry, dict):
        return None
    try:
        ts = float(entry.get("ts", 0))
    except (TypeError, ValueError):
        return None
    if now - ts > WINDOW_SECONDS:
        return None
    result = entry.get("result")
    return result if isinstance(result, dict) else None


def record(state_dir, tool: str, key: str, result: dict, now: float | None = None) -> None:
    """Сохранить первый результат; записи вне окна вычищаются заодно."""
    now = time.time() if now is None else now
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    data = _load(state_dir)
    data = {
        k: v for k, v in data.items()
        if isinstance(v, dict) and (now - float(v.get("ts") or 0)) <= WINDOW_SECONDS
    }
    data[_entry_id(tool, key)] = {"ts": now, "result": result}
    path = registry_path(state_dir)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    os.chmod(path, 0o600)
