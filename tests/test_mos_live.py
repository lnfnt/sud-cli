"""mos-gorsud live adapter tests (fake HTTP, no network)."""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.mos import cache as mos_cache  # noqa: E402
from mos.live import (MosLiveError,  # noqa: E402
                      get_mos_case, mos_card, mos_search)


@pytest.fixture(autouse=True)
def fresh_cache(tmp_path, monkeypatch):
    """Redirect the module-level default cache to a temp db."""
    db = mos_cache.MosCache(tmp_path / "mos.db")
    monkeypatch.setattr(mos_cache, "_DEFAULT_CACHE", db)
    yield

SEARCH_HTML = ('<a href="/rs/horoshevskij/services/cases/civil/details/'
               'adf460a1-b12b-11f1-b669-2f14c19231b4">x</a>')

CARD_HTML = """
<html><body>
<div class="row"><div class="left">Стороны</div><div class="right">
<p class="table-bold-text">Истец</p>Мачавариани С.З.<br/>
<p class="table-bold-text">Ответчик</p>АО &quot;АльфаСтрахование&quot;<br/>
</div></div>
<div class="row"><div class="left">Cудья</div><div class="right">Черныш В.В.</div></div>
<div class="row"><div class="left">Текущее состояние</div><div class="right">Зарегистрировано, 15.09.2026</div></div>
<table><tr><th>История состояний</h3></tr></table><table><tbody>
<tr><td>10.09.2026</td><td>Регистрация поступившего заявления</td><td></td></tr>
<tr><td>15.09.2026</td><td>Заявление принято к производству</td><td></td></tr>
</tbody></table>
<table><tr><th>Документы из описи</th></tr></table><table><tbody></tbody></table>
</body></html>"""


class FakeHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, url, *, proxy=None, timeout=30):
        self.calls.append(url)
        if "/search?" in url:
            return SEARCH_HTML
        if "/details/" in url:
            return CARD_HTML
        return "<html>404</html>"


class SearchTests(unittest.TestCase):
    def test_search_finds_card_link(self):
        fake = FakeHttp()
        with patch("mos.live._http_get", fake):
            hits = mos_search("horoshevskij", "02-2101/2026")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "civil")
        self.assertEqual(hits[0]["uuid"],
                         "adf460a1-b12b-11f1-b669-2f14c19231b4")

    def test_search_no_results(self):
        class Empty(FakeHttp):
            def __call__(self, url, **kw):
                return "<html>ничего не найдено</html>"
        with patch("mos.live._http_get", Empty()):
            hits = mos_search("horoshevskij", "99-9999/2030")
        self.assertEqual(hits, [])


class CardTests(unittest.TestCase):
    def test_card_parse(self):
        fake = FakeHttp()
        with patch("mos.live._http_get", fake):
            card = mos_card("horoshevskij",
                            "adf460a1-b12b-11f1-b669-2f14c19231b4")
        self.assertEqual(card["judge"], "Черныш В.В.")
        self.assertEqual(len(card["parties"]), 2)
        self.assertEqual(card["parties"][0]["role"], "Истец")
        self.assertEqual(card["parties"][0]["name"], "Мачавариани С.З.")
        self.assertEqual(card["parties"][1]["name"], 'АО "АльфаСтрахование"')
        self.assertEqual(len(card["motion"]), 2)
        self.assertEqual(card["motion"][0]["date"], "10.09.2026")

    def test_full_pipeline(self):
        with patch("mos.live._http_get", FakeHttp()):
            result = get_mos_case("horoshevskij", "02-2101/2026")
        self.assertEqual(result["case_number_query"], "02-2101/2026")
        self.assertEqual(len(result["motion"]), 2)

    def test_not_found_raises(self):
        class Empty(FakeHttp):
            def __call__(self, url, **kw):
                return "<html>ничего не найдено</html>"
        with patch("mos.live._http_get", Empty()):
            with self.assertRaises(MosLiveError):
                get_mos_case("horoshevskij", "99-9999/2030")


if __name__ == "__main__":
    unittest.main()
