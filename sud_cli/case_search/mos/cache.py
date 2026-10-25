"""SQLite cache for Mos-gorsud live searches and case cards.

Thin, stdlib-only persistence used by ``case_search.mos.live``.
Cache errors are logged (and swallowed) by the caller in ``live.py`` so
that a broken cache never breaks a fetch.

Empty search results are stored (so we have a record of the fetch) but
are never returned from the cache. This avoids cache poisoning from
"no results" pages while the case is still being registered or indexed.
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
    return default_storage_root() / "mos" / "mos.db"


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


class MosCache:
    """Thread-safe SQLite cache for mos-gorsud searches and case cards."""

    SEARCH_TTL_DAYS = 1
    CARD_TTL_DAYS = 7

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
                CREATE TABLE IF NOT EXISTS searches (
                    slug TEXT NOT NULL,
                    query TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    PRIMARY KEY(slug, query)
                );
                CREATE TABLE IF NOT EXISTS cards (
                    uuid TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    html TEXT NOT NULL
                );
                """
            )

    def get_cached_search(
        self,
        slug: str,
        query: str,
        ttl_days: int = SEARCH_TTL_DAYS,
        allow_stale: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Return cached search results if fresh and non-empty."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, results_json FROM searches "
                "WHERE slug = ? AND query = ?",
                (slug, query),
            )
            row = cur.fetchone()
        if row is None:
            return None
        fetched_at, results_json = row
        if not _is_fresh(fetched_at, ttl_days) and not allow_stale:
            return None
        try:
            results = json.loads(results_json)
            if not isinstance(results, list):
                return None
            # Empty results are never served from cache to avoid poisoning.
            if len(results) == 0:
                return None
            return [dict(r) for r in results]
        except Exception:
            return None

    def put_search(
        self,
        slug: str,
        query: str,
        results: list[dict[str, Any]],
    ) -> None:
        """Store search results. Empty results are stored for audit only."""
        results_json = json.dumps([dict(r) for r in results])
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO searches(slug, query, fetched_at, results_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(slug, query) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    results_json = excluded.results_json
                """,
                (slug, query, _utc_now(), results_json),
            )

    def get_cached_card(
        self,
        uuid: str,
        ttl_days: int = CARD_TTL_DAYS,
        allow_stale: bool = False,
    ) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fetched_at, html FROM cards WHERE uuid = ?",
                (uuid,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        fetched_at, html = row
        if not _is_fresh(fetched_at, ttl_days) and not allow_stale:
            return None
        return html

    def put_card(self, uuid: str, html: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cards(uuid, fetched_at, html)
                VALUES(?, ?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    html = excluded.html
                """,
                (uuid, _utc_now(), html),
            )

    def cache_stats(self) -> dict[str, Any]:
        with self._lock:
            total_searches = self._conn.execute(
                "SELECT COUNT(*) FROM searches").fetchone()[0]
            total_cards = self._conn.execute(
                "SELECT COUNT(*) FROM cards").fetchone()[0]
            stale_searches = 0
            stale_cards = 0
            for (fetched_at,) in self._conn.execute(
                    "SELECT fetched_at FROM searches"):
                if not _is_fresh(fetched_at, self.SEARCH_TTL_DAYS):
                    stale_searches += 1
            for (fetched_at,) in self._conn.execute(
                    "SELECT fetched_at FROM cards"):
                if not _is_fresh(fetched_at, self.CARD_TTL_DAYS):
                    stale_cards += 1
        return {
            "total_searches": total_searches,
            "total_cards": total_cards,
            "stale_searches": stale_searches,
            "stale_cards": stale_cards,
            "db_path": str(self.db_path),
        }


_DEFAULT_CACHE: MosCache | None = None


def _get_default_cache() -> MosCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = MosCache()
    return _DEFAULT_CACHE


def get_cached_search(
    slug: str,
    query: str,
    ttl_days: int = MosCache.SEARCH_TTL_DAYS,
    allow_stale: bool = False,
) -> list[dict[str, Any]] | None:
    return _get_default_cache().get_cached_search(
        slug, query, ttl_days, allow_stale)


def put_search(
    slug: str,
    query: str,
    results: list[dict[str, Any]],
) -> None:
    _get_default_cache().put_search(slug, query, results)


def get_cached_card(
    uuid: str,
    ttl_days: int = MosCache.CARD_TTL_DAYS,
    allow_stale: bool = False,
) -> str | None:
    return _get_default_cache().get_cached_card(
        uuid, ttl_days, allow_stale)


def put_card(uuid: str, html: str) -> None:
    _get_default_cache().put_card(uuid, html)


def cache_stats() -> dict[str, Any]:
    return _get_default_cache().cache_stats()
