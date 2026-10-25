"""история §5 (S-85): backup / verify / restore snapshot'а store.db.

sqlite stdlib, без внешних процессов:
- create — ``sqlite3.Connection.backup`` (горячий снимок, не cp под WAL),
  файл 0600 + sha256 sidecar; путь снимка пишется в ``meta`` (S-86 читает
  «last backup» оттуда, а не из воздуха);
- verify — PRAGMA integrity_check + quick_check + список таблиц схемы;
  sidecar сверяется, если лежит рядом;
- restore — деструктив: без явного подтверждения не делает ничего, превью
  показывает, что будет перезаписано (путь, размер, таблица->кол-во строк
  текущей БД). Живой state в тестах не трогается: вызывающий передаёт tmp-dir.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .db import _db_path, get_connection, init_db, set_meta

BACKUP_PREFIX = "store-backup-"
SIDECAR_SUFFIX = ".sha256"
META_LAST_BACKUP_KEY = "last_backup_path"
META_LAST_BACKUP_SHA_KEY = "last_backup_sha256"

RESTORE_DISCLAIMER = (
    "restore перезаписывает store.db state-dir целиком: всё, что было создано "
    "после снимка, теряется. Подтверждение --yes обязательно."
)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def default_backup_dir(state_dir: Any) -> Path:
    return Path(state_dir) / "backups"


def snapshot_tables(path: Path) -> Dict[str, int]:
    """таблица -> число строк (только пользовательские таблицы, без sqlite_INTERNAL)."""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
        ).fetchall()
        out = {}
        for (name,) in rows:
            out[name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        return out
    finally:
        conn.close()


def create_backup(state_dir: Any, out: Optional[Path] = None) -> Dict[str, Any]:
    """Горячий снимок store.db -> timestamped файл 0600 + sha256 sidecar."""
    state_dir = Path(state_dir)
    init_db(state_dir)
    src_path = _db_path(state_dir)
    if not src_path.is_file():
        raise FileNotFoundError(f"store.db не найден: {src_path}")

    if out is None:
        dest_dir = default_backup_dir(state_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{BACKUP_PREFIX}{_now_stamp()}.db"
    else:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(target))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # 0600 до того, как появится sidecar: снимок содержит ПДн дела.
    os.chmod(target, 0o600)
    digest = _sha256_file(target)
    sidecar = target.with_name(target.name + SIDECAR_SUFFIX)
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    os.chmod(sidecar, 0o600)

    conn = get_connection(state_dir)
    try:
        set_meta(conn, META_LAST_BACKUP_KEY, str(target))
        set_meta(conn, META_LAST_BACKUP_SHA_KEY, digest)
    finally:
        conn.close()

    return {
        "path": str(target),
        "sidecar": str(sidecar),
        "sha256": digest,
        "bytes": target.stat().st_size,
        "mode": oct(0o600),
        "tables": snapshot_tables(target),
        "source": str(src_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def read_sidecar(backup: Path) -> Optional[str]:
    sidecar = backup.with_name(backup.name + SIDECAR_SUFFIX)
    if not sidecar.is_file():
        return None
    text = sidecar.read_text(encoding="utf-8").strip()
    return text.split()[0] if text else None


def verify_backup(path: Any) -> Dict[str, Any]:
    """integrity_check + quick_check + таблицы схемы; sidecar, если есть рядом."""
    backup = Path(path)
    if not backup.is_file():
        raise FileNotFoundError(f"backup не найден: {backup}")
    integrity = quick = None
    conn = sqlite3.connect(str(backup))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name ASC").fetchall()]
        schema_version = None
        if "meta" in tables:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            schema_version = row[0] if row else None
    finally:
        conn.close()

    recorded = read_sidecar(backup)
    actual = _sha256_file(backup)
    return {
        "path": str(backup),
        "bytes": backup.stat().st_size,
        "integrity_check": integrity,
        "quick_check": quick,
        "integrity_ok": integrity == "ok",
        "quick_ok": quick == "ok",
        "tables": tables,
        "table_count": len(tables),
        "schema_version": schema_version,
        "sha256": actual,
        "sidecar_sha256": recorded,
        "sidecar_match": None if recorded is None else (recorded == actual),
    }


def restore_preview(state_dir: Any, path: Any) -> Dict[str, Any]:
    """Что будет перезаписано: путь живой БД, её размер/таблицы + снимок."""
    state_dir = Path(state_dir)
    live = _db_path(state_dir)
    info: Dict[str, Any] = {
        "target": str(live),
        "target_exists": live.is_file(),
        "target_bytes": live.stat().st_size if live.is_file() else 0,
        "target_tables": snapshot_tables(live) if live.is_file() else {},
        "source": str(path),
        "source_tables": {},
    }
    if Path(path).is_file():
        info["source_tables"] = snapshot_tables(Path(path))
    return info


def restore_backup(state_dir: Any, path: Any, confirmed: bool = False,
                   actor: str = "cli") -> Dict[str, Any]:
    """Перезапись store.db из снимка. Без confirmed ничего не меняется."""
    state_dir = Path(state_dir)
    backup = Path(path)
    preview = restore_preview(state_dir, backup)
    if not confirmed:
        return {
            "status": "needs_confirmation",
            "detail": "restore деструктивен: требуется --yes; изменится store.db "
                      "целиком — всё, что создано после снимка, будет потеряно",
            "preview": preview,
            "disclaimer": RESTORE_DISCLAIMER,
        }
    if not backup.is_file():
        raise FileNotFoundError(f"backup не найден: {backup}")
    check = verify_backup(backup)
    if not (check["integrity_ok"] and check["quick_ok"]):
        raise sqlite3.DatabaseError(
            f"backup не прошёл integrity_check/quick_check: "
            f"{check['integrity_check']}/{check['quick_check']}")

    state_dir.mkdir(parents=True, exist_ok=True)
    target = _db_path(state_dir)
    src = sqlite3.connect(str(backup))
    try:
        dst = sqlite3.connect(str(target))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(target, 0o600)
    conn = get_connection(state_dir)
    try:
        set_meta(conn, "last_restore_at", datetime.now(timezone.utc).isoformat())
        set_meta(conn, "last_restore_source", str(backup))
    finally:
        conn.close()
    return {
        "status": "restored",
        "target": str(target),
        "source": str(backup),
        "actor": actor,
        "tables_after": snapshot_tables(target),
        "disclaimer": RESTORE_DISCLAIMER,
    }


def last_backup(state_dir: Any) -> Dict[str, Any]:
    """Последний снимок из meta (S-86): путь+sha, которые мы сами записали."""
    state_dir = Path(state_dir)
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        from .db import get_meta

        path = get_meta(conn, META_LAST_BACKUP_KEY)
        sha = get_meta(conn, META_LAST_BACKUP_SHA_KEY)
    finally:
        conn.close()
    exists = bool(path) and Path(str(path)).is_file()
    recorded = read_sidecar(Path(str(path))) if exists else None
    return {
        "path": path,
        "sha256": sha,
        "exists": exists,
        # verified — sidecar рядом и совпал с записанным в meta; None, если
        # снимка нет: «не проверялось», а не «проверено и плохо».
        "verified": None if not exists else bool(recorded and recorded == sha),
    }
