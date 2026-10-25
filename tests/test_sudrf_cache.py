"""Tests for case_search.sudrf.cache and live.py cache integration.

All network calls are mocked; no real HTTP traffic leaves the runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.sudrf import cache  # noqa: E402
from case_search.sudrf import live  # noqa: E402
from case_search.sudrf.live import (  # noqa: E402
    fetch_listing,
    fetch_case_card,
    lookup_case_number,
)


LISTING_HTML = (
    "<a href='/modules.php?name=sud_delo&srv_num=1&name_op=case&"
    "case_id=268838394&case_uid=a63d066e-b906-42e1-80e2-0dfa9dc8ea21&"
    "delo_id=5&new=5'><u>33-35073/2026</u></a><td>09:20</td>"
)

CARD_HTML = (
    "<!--" + "x" * 11000 + "-->"
    "<html><body><table id='tablcont'><tr><th>ДЕЛО</th></tr>\n"
    "<tr><td><b>Уникальный идентификатор дела</b></td>"
    "<td>50RS0019-01-2025-000294-14</td></tr>\n"
    "<tr><td><b>Судья</b></td><td>Тюшляева Н.В.</td></tr>\n"
    "</table>\n"
    "<table><tr><th>ДВИЖЕНИЕ ДЕЛА</th></tr>\n"
    "<tr><td align='center'><b>Наименование события</b></td>"
    "<td><b>Дата</b></td></tr>\n"
    "<tr><td>Судебное заседание</td><td>16.09.2026</td></tr>\n"
    "</table>\n"
    "<table><tr><th>УЧАСТНИКИ</th></tr>\n"
    "<tr><td><b>Вид лица, участвующего в деле</b></td>"
    "<td><b>Фамилия</b></td></tr>\n"
    "<tr><td>ОТВЕТЧИК</td><td>ООО Ромашка</td></tr>\n"
    "</table></body></html>"
)


class HttpTracker:
    """Return canned bodies and record every URL passed to _http_get."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str, *, proxy=None, timeout=30):
        self.calls.append(url)
        if "H_date=" in url:
            return 200, LISTING_HTML
        if "name_op=case" in url:
            return 200, CARD_HTML
        return 200, LISTING_HTML


@pytest.fixture(autouse=True)
def fresh_cache(tmp_path, monkeypatch):
    """Redirect the module-level default cache to a temp db."""
    db = cache.ListingCache(tmp_path / "listings.db")
    monkeypatch.setattr(cache, "_DEFAULT_CACHE", db)
    yield
    # monkeypatch restores the original _DEFAULT_CACHE after the test.


def test_fetch_stores_listing_in_cache(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(live, "_http_get", tracker)

    rows = fetch_listing("oblsud.mo.sudrf.ru", date="16.09.2026")

    assert len(rows) == 1
    assert rows[0].case_number == "33-35073/2026"
    stats = cache.cache_stats()
    assert stats["total_listings"] == 1
    assert stats["total_cards"] == 0


def test_second_fetch_hits_cache(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(live, "_http_get", tracker)

    rows1 = fetch_listing("oblsud.mo.sudrf.ru", date="16.09.2026")
    rows2 = fetch_listing("oblsud.mo.sudrf.ru", date="16.09.2026")

    assert rows1 == rows2
    assert len(tracker.calls) == 1


def test_stale_listing_refetched(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(live, "_http_get", tracker)

    # Seed a stale empty listing directly into the cache.
    db = cache._get_default_cache()
    db.put_listing(
        "oblsud.mo.sudrf.ru",
        "16.09.2026",
        "https://oblsud.mo.sudrf.ru/modules.php?name=sud_delo",
        [],
    )
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE listings SET fetched_at = ? WHERE host = ? AND date = ?",
            ("2020-01-01T00:00:00+00:00", "oblsud.mo.sudrf.ru",
             "16.09.2026"),
        )

    rows = fetch_listing("oblsud.mo.sudrf.ru", date="16.09.2026")

    assert len(rows) == 1
    assert rows[0].case_number == "33-35073/2026"
    assert len(tracker.calls) == 1


def test_fetch_case_card_cached(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(live, "_http_get", tracker)

    html1 = fetch_case_card(
        "oblsud.mo.sudrf.ru", "268838394",
        "a63d066e-b906-42e1-80e2-0dfa9dc8ea21")
    html2 = fetch_case_card(
        "oblsud.mo.sudrf.ru", "268838394",
        "a63d066e-b906-42e1-80e2-0dfa9dc8ea21")

    assert html1 == html2
    assert len(tracker.calls) == 1
    assert cache.cache_stats()["total_cards"] == 1


def test_parallel_lookup_returns_correct_rows(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(live, "_http_get", tracker)

    rows = lookup_case_number(
        "33-35073/2026",
        host="oblsud.mo.sudrf.ru",
        date_window=3,
        explicit_dates=["14.09.2026"],
        max_workers=4,
    )

    assert len(rows) == 1
    assert rows[0].case_number == "33-35073/2026"
    # One HTTP call per unique scanned date; parallelism is exercised
    # but correctness is verified by the returned rows.
    assert len(tracker.calls) == 4
    assert all("H_date=" in u for u in tracker.calls)


def test_cache_error_degrades_to_fetch(monkeypatch):
    """A broken cache must not break the fetch path."""

    class Broken:
        def get_cached_listing(self, *a, **kw):
            raise RuntimeError("boom")

        def put_listing(self, *a, **kw):
            raise RuntimeError("boom")

        def get_cached_card(self, *a, **kw):
            raise RuntimeError("boom")

        def put_card(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(cache, "_DEFAULT_CACHE", Broken())
    tracker = HttpTracker()
    monkeypatch.setattr(live, "_http_get", tracker)

    rows = fetch_listing("oblsud.mo.sudrf.ru", date="16.09.2026")
    assert len(rows) == 1
    assert len(tracker.calls) == 1

    card = fetch_case_card(
        "oblsud.mo.sudrf.ru", "268838394",
        "a63d066e-b906-42e1-80e2-0dfa9dc8ea21")
    assert CARD_HTML in card
    assert len(tracker.calls) == 2
