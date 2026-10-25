"""SQLite cache for SudRF live listings and case cards.

Thin, stdlib-only persistence used by ``case_search.sudrf.live``.
Cache errors are logged (and swallowed) by the caller in ``live.py`` so
that a broken cache never breaks a fetch.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from case_search.storage import default_storage_root


def _default_db_path() -> Path:
    return default_storage_root() / "sudrf" / "listings.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_fresh(fetched_at: str, ttl_days: int) -> bool:
    try:
        dt = datetime.fromisoformat(fetched_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt <= timedelta(days=ttl_days)
    except Exception:
        return False


def _row_to_dict(row: Any) -> dict[str, str]:
    if isinstance(row, dict):
        return {str(k): str(v) for k, v in row.items()}
    if hasattr(row, "to_dict"):
        return {str(k): str(v) for k, v in row.to_dict().items()}
    raise TypeError(f"cannot serialize listing row of type {type(row)}")


class ListingCache:
    """Thread-safe SQLite cache for sudrf listings and case cards."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    host TEXT NOT NULL,
                    date TEXT NOT NULL,
                    url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    PRIMARY KEY(host, date)
                );
                CREATE TABLE IF NOT EXISTS cards (
                    case_uid TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    html TEXT NOT NULL
                );
                """
            )

    def get_cached_listing(
        self,
        host: str,
        date: str,
        ttl_days: int = 7,
        allow_stale: bool = False,
    ) -> list[dict[str, str]] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, rows_json FROM listings "
                "WHERE host = ? AND date = ?",
                (host, date),
            )
            row = cur.fetchone()
        if row is None:
            return None
        fetched_at, rows_json = row
        if not _is_fresh(fetched_at, ttl_days) and not allow_stale:
            return None
        try:
            rows = json.loads(rows_json)
            if not isinstance(rows, list):
                return None
            return [dict(r) for r in rows]
        except Exception:
            return None

    def put_listing(
        self,
        host: str,
        date: str,
        url: str,
        rows: list[dict[str, str]] | list[Any],
    ) -> None:
        rows_json = json.dumps([_row_to_dict(r) for r in rows])
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO listings(host, date, url, fetched_at, rows_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(host, date) DO UPDATE SET
                    url = excluded.url,
                    fetched_at = excluded.fetched_at,
                    rows_json = excluded.rows_json
                """,
                (host, date, url, _utc_now(), rows_json),
            )

    def get_cached_card(
        self,
        case_uid: str,
        ttl_days: int = 30,
        allow_stale: bool = False,
    ) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, html FROM cards WHERE case_uid = ?",
                (case_uid,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        fetched_at, html = row
        if not _is_fresh(fetched_at, ttl_days) and not allow_stale:
            return None
        return html

    def put_card(self, case_uid: str, host: str, html: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cards(case_uid, host, fetched_at, html)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(case_uid) DO UPDATE SET
                    host = excluded.host,
                    fetched_at = excluded.fetched_at,
                    html = excluded.html
                """,
                (case_uid, host, _utc_now(), html),
            )

    def cache_stats(self) -> dict[str, Any]:
        with self._lock:
            total_listings = self._conn.execute(
                "SELECT COUNT(*) FROM listings").fetchone()[0]
            total_cards = self._conn.execute(
                "SELECT COUNT(*) FROM cards").fetchone()[0]
            stale_listings = 0
            stale_cards = 0
            for (fetched_at,) in self._conn.execute(
                    "SELECT fetched_at FROM listings"):
                if not _is_fresh(fetched_at, 7):
                    stale_listings += 1
            for (fetched_at,) in self._conn.execute(
                    "SELECT fetched_at FROM cards"):
                if not _is_fresh(fetched_at, 30):
                    stale_cards += 1
        return {
            "total_listings": total_listings,
            "total_cards": total_cards,
            "stale_listings": stale_listings,
            "stale_cards": stale_cards,
            "db_path": str(self.db_path),
        }


_DEFAULT_CACHE: ListingCache | None = None


def _get_default_cache() -> ListingCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = ListingCache()
    return _DEFAULT_CACHE


def get_cached_listing(
    host: str,
    date: str,
    ttl_days: int = 7,
    allow_stale: bool = False,
) -> list[dict[str, str]] | None:
    return _get_default_cache().get_cached_listing(
        host, date, ttl_days, allow_stale)


def put_listing(
    host: str,
    date: str,
    url: str,
    rows: list[dict[str, str]] | list[Any],
) -> None:
    _get_default_cache().put_listing(host, date, url, rows)


def get_cached_card(
    case_uid: str,
    ttl_days: int = 30,
    allow_stale: bool = False,
) -> str | None:
    return _get_default_cache().get_cached_card(
        case_uid, ttl_days, allow_stale)


def put_card(case_uid: str, host: str, html: str) -> None:
    _get_default_cache().put_card(case_uid, host, html)


def cache_stats() -> dict[str, Any]:
    return _get_default_cache().cache_stats()
