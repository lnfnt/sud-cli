"""Tests for case_search.mos.cache and live.py cache integration.

All network calls are mocked; no real HTTP traffic leaves the runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.mos import cache as mos_cache  # noqa: E402
from mos import live as mos_live  # noqa: E402


SEARCH_HTML = (
    '<a href="/rs/horoshevskij/services/cases/civil/details/'
    'adf460a1-b12b-11f1-b669-2f14c19231b4">x</a>'
)

CARD_HTML = """<html><body>
<div class="row"><div class="left">Стороны</div><div class="right">
<p class="table-bold-text">Истец</p>Мачавариани С.З.<br/>
<p class="table-bold-text">Ответчик</p>АО &quot;АльфаСтрахование&quot;<br/>
</div></div>
<div class="row"><div class="left">Cудья</div><div class="right">Черныш В.В.</div></div>
<div class="row"><div class="left">Текущее состояние</div><div class="right">Зарегистрировано, 15.09.2026</div></div>
<table><tr><th>История состояний</h3></tr></table><table><tbody>
<tr><td>10.09.2026</td><td>Регистрация поступившего заявления</td><td></td></tr>
</tbody></table>
<table><tr><th>Документы из описи</th></tr></table><table><tbody></tbody></table>
</body></html>"""

EMPTY_SEARCH_HTML = "<html>ничего не найдено</html>"

UUID = "adf460a1-b12b-11f1-b669-2f14c19231b4"


class HttpTracker:
    """Return canned bodies and record every URL passed to _http_get."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str, *, proxy=None, timeout=30):
        self.calls.append(url)
        if "/search?" in url:
            return SEARCH_HTML
        if "/details/" in url:
            return CARD_HTML
        return "<html>404</html>"


@pytest.fixture(autouse=True)
def fresh_cache(tmp_path, monkeypatch):
    """Redirect the module-level default cache to a temp db."""
    db = mos_cache.MosCache(tmp_path / "mos.db")
    monkeypatch.setattr(mos_cache, "_DEFAULT_CACHE", db)
    yield
    # monkeypatch restores the original _DEFAULT_CACHE after the test.


def test_search_stores_in_cache(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(mos_live, "_http_get", tracker)

    hits = mos_live.mos_search("horoshevskij", "02-2101/2026")

    assert len(hits) == 1
    assert hits[0]["uuid"] == UUID
    stats = mos_cache.cache_stats()
    assert stats["total_searches"] == 1
    assert stats["total_cards"] == 0


def test_second_search_hits_cache(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(mos_live, "_http_get", tracker)

    hits1 = mos_live.mos_search("horoshevskij", "02-2101/2026")
    hits2 = mos_live.mos_search("horoshevskij", "02-2101/2026")

    assert hits1 == hits2
    assert len(tracker.calls) == 1


def test_empty_cached_search_result_is_refetched(monkeypatch):
    """Empty cached search results must be refetched, not served."""
    tracker = HttpTracker()
    monkeypatch.setattr(mos_live, "_http_get", tracker)

    # Seed an empty cached result directly.
    db = mos_cache._get_default_cache()
    db.put_search("horoshevskij", "02-2101/2026", [])

    hits = mos_live.mos_search("horoshevskij", "02-2101/2026")

    assert len(hits) == 1
    assert hits[0]["uuid"] == UUID
    # One call for the refetch; empty cache did not satisfy the read.
    assert len(tracker.calls) == 1


def test_card_cached(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(mos_live, "_http_get", tracker)

    card1 = mos_live.mos_card("horoshevskij", UUID)
    card2 = mos_live.mos_card("horoshevskij", UUID)

    assert card1["judge"] == card2["judge"]
    assert len(tracker.calls) == 1
    assert mos_cache.cache_stats()["total_cards"] == 1


def test_card_ttl_expiry_refetch(monkeypatch):
    tracker = HttpTracker()
    monkeypatch.setattr(mos_live, "_http_get", tracker)

    # Seed a stale cached card directly.
    db = mos_cache._get_default_cache()
    db.put_card(UUID, CARD_HTML)
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE cards SET fetched_at = ? WHERE uuid = ?",
            ("2020-01-01T00:00:00+00:00", UUID),
        )

    card = mos_live.mos_card("horoshevskij", UUID)

    assert card["judge"] == "Черныш В.В."
    assert len(tracker.calls) == 1


def test_cache_error_degrades_to_fetch(monkeypatch):
    """A broken cache must not break the fetch path."""

    class Broken:
        def get_cached_search(self, *a, **kw):
            raise RuntimeError("boom")

        def put_search(self, *a, **kw):
            raise RuntimeError("boom")

        def get_cached_card(self, *a, **kw):
            raise RuntimeError("boom")

        def put_card(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(mos_cache, "_DEFAULT_CACHE", Broken())
    tracker = HttpTracker()
    monkeypatch.setattr(mos_live, "_http_get", tracker)

    hits = mos_live.mos_search("horoshevskij", "02-2101/2026")
    assert len(hits) == 1
    assert hits[0]["uuid"] == UUID
    assert len(tracker.calls) == 1

    card = mos_live.mos_card("horoshevskij", UUID)
    assert card["judge"] == "Черныш В.В."
    assert len(tracker.calls) == 2
