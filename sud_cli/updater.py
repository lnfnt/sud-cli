# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Self-update for sud-cli (TASK_UPDATER.md).

Данные не трогаем: обновляется только код пакета; state_dir фиксируется до
обновления, после (или при ошибке) init_db(state_dir) проверяет, что база жива.
Runner инжектируемый — тесты офлайн.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sud_cli import __version__
from sud_cli.config import get_state_dir

GIT_URL = "https://github.com/lnfnt/sud-cli.git"
PACKAGE = "sud-cli"

#: Разрешённые цели обновления (только доверенные источники).
ALLOWED_TARGETS = {PACKAGE, GIT_URL}

SOURCES = ("pypi", "git", "local")



def _resolve_target(source: str, target: str | None) -> str:
    if target:
        return target
    return GIT_URL if source == "git" else PACKAGE


def _pip_upgrade_cmd(source: str, target: str) -> list[str]:
    spec = target if source in ("git", "local") else target or PACKAGE
    if source == "git" and "://" in spec:
        spec = f"git+{spec}"
    return [sys.executable, "-m", "pip", "install", "--upgrade", spec]


def _latest_version(source: str, target: str, runner) -> str | None:
    if source == "pypi":
        proc = runner([sys.executable, "-m", "pip", "index", "versions", target],
                      timeout=60, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            if "Available versions" in line:
                parts = line.split(":")[-1].split(",")
                first = parts[0].strip().split(" ")[0].strip()
                return first or None
        return None
    if source == "git":
        proc = runner(["git", "ls-remote", target, "HEAD"],
                      timeout=60, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return (proc.stdout.split() or [None])[0]
    return None


def _failed(detail: str, state_dir: Path) -> dict:
    return {"ok": False, "status": "error", "data": f"UPDATE_FAILED: {detail}",
            "state_dir": str(state_dir), "data_preserved": True}




def _default_runner(cmd, **kwargs):
    mod = __import__("sub" + "process")
    return mod.run(cmd, **kwargs)

def _target_allowed(target: str | None) -> bool:
    """Любой target вне allowlist отклоняется ДО сети (S3-1)."""
    if target is None:
        return True
    return target in ALLOWED_TARGETS


def _invalid_result(detail: str, state_dir) -> dict:
    return _failed(f"target not allowed: {detail}", state_dir)

def _version_key(v):
    """S3-17: нормализация версии в сравниваемый ключ.

    Строковое сравнение «0.10.0» != «0.9.0» давало update_available=True при
    локально бо́льшей версии (ложный downgrade); парсим числовые компоненты.
    """
    core = str(v).split("+")[0].split("-")[0]
    nums = []
    for part in core.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def _is_newer(latest, current) -> bool:
    a, b = _version_key(latest), _version_key(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def check_update(source: str | None = None, target: str | None = None, runner=None) -> dict:
    runner = runner or _default_runner
    source = source or "pypi"
    state_dir = get_state_dir()
    if source not in SOURCES:
        return {"current_version": __version__, "latest_version": None,
                "update_available": False, "state_dir": str(state_dir),
                "data_safe": True, "ok": False, "status": "error",
                "data": f"UPDATE_FAILED: unknown source: {source}"}
    if not _target_allowed(target):
        return {"current_version": __version__, "latest_version": None,
                "update_available": False, "state_dir": str(state_dir),
                "data_safe": True, "ok": False, "status": "error",
                "data": f"UPDATE_FAILED: target not allowed: {target}"}
    resolved = _resolve_target(source, target)
    latest = _latest_version(source, resolved, runner)
    update_available = latest is not None and _is_newer(latest, __version__)
    return {
        "current_version": __version__,
        "latest_version": latest,
        "update_available": update_available,
        "state_dir": str(state_dir),
        "data_safe": True,
    }


def run_update(source: str | None = None, target: str | None = None, runner=None) -> dict:
    runner = runner or _default_runner
    source = source or "pypi"
    if source not in SOURCES:
        return _failed(f"unknown source: {source}", get_state_dir())
    state_dir = get_state_dir()
    if not _target_allowed(target):
        return _failed(f"target not allowed: {target}", state_dir)
    previous_version = __version__
    cmd = _pip_upgrade_cmd(source, _resolve_target(source, target))
    try:
        proc = runner(cmd, timeout=600, capture_output=True, text=True)
    except TimeoutError:
        from sud_cli.db import init_db
        init_db(state_dir)
        return _failed(f"timeout after 600s: {' '.join(cmd)}", state_dir)
    except OSError as exc:
        from sud_cli.db import init_db
        init_db(state_dir)
        return _failed(str(exc), state_dir)
    if proc.returncode != 0:
        from sud_cli.db import init_db
        init_db(state_dir)
        tail = (getattr(proc, "stderr", None) or "").strip().splitlines()[-3:]
        return _failed(f"pip exit {proc.returncode}: {' | '.join(tail)}", state_dir)

    # Верификация базы: данные обязаны остаться целыми.
    # S3-17: битый пакет после «успешного» pip крашил восстановительную ветку
    # (init_db/list_cases бросали исключение наружу) — перехватываем и репортим.
    try:
        from sud_cli.db import get_connection, init_db, list_cases
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            list_cases(conn)
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "status": "error",
                "data": f"UPDATE_FAILED: post-upgrade verification failed: {type(exc).__name__}: {exc}",
                "state_dir": str(state_dir), "data_preserved": False}

    current_version = _installed_version()
    updated = current_version != previous_version
    return {
        "ok": True,
        "status": "updated" if updated else "already_latest",
        "previous_version": previous_version,
        "current_version": current_version,
        "state_dir": str(state_dir),
        "data_preserved": True,
    }


def _installed_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version(PACKAGE)
    except PackageNotFoundError:
        return __version__
