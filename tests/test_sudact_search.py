"""SudAct practice search tests (live fixture + fail-closed)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (EmptyPayloadError,  # noqa: E402
                                  WafDetectedError)
from case_search.sudact.search import (build_search_url,  # noqa: E402
                                         parse_search_html, search)
from case_search import get_practice_doc  # noqa: E402

FIXTURE = (REPO_ROOT / "fixtures" / "sudact"
           / "search-results-arbitral.html")
HTML = FIXTURE.read_text(encoding="utf-8", errors="replace")


class ParseSearchTests(unittest.TestCase):
    def test_live_fixture_parses_30_results(self):
        res = parse_search_html(HTML, limit=30, query="договор подряда",
                                section="arbitral", page=1,
                                source_url="https://sudact.ru/arbitral/")
        d = res.to_dict()
        self.assertEqual(d["count"], 30)
        self.assertEqual(d["next_page"], 2)
        first = d["results"][0]
        self.assertEqual(first["position"], 1)
        self.assertTrue(first["url"].startswith("https://sudact.ru/"))
        self.assertTrue(first["court"])
        self.assertTrue(first["title"])

    def test_every_result_has_url_and_title(self):
        res = parse_search_html(HTML, limit=30, query="q", section="arbitral",
                                page=1, source_url="u")
        for r in res.results:
            self.assertIn("/doc/", r["url"])
            self.assertTrue(r["title"])


class FailClosedTests(unittest.TestCase):
    def test_empty_page_raises(self):
        with self.assertRaises(EmptyPayloadError):
            parse_search_html("  ", query="q", section="arbitral",
                              page=1, source_url="u")

    def test_challenge_raises_waf(self):
        with self.assertRaises(WafDetectedError):
            parse_search_html("<html>captcha доступ ограничен</html>",
                              query="q", section="arbitral", page=1,
                              source_url="u")

    def test_no_results_raises(self):
        with self.assertRaises(EmptyPayloadError):
            parse_search_html("<html><body>ничего не найдено</body>"
                              "</html>", query="q", section="arbitral",
                              page=1, source_url="u")

    def test_fetcher_failure_is_error(self):
        from case_search.sudact.search import FetchError

        def bad_fetcher(url):
            raise OSError("network down")
        with self.assertRaises(FetchError):
            search("q", bad_fetcher)

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            search("  ", lambda u: "<html></html>")


class UrlTests(unittest.TestCase):
    def test_build_search_url(self):
        url = build_search_url("подряд", "arbitral", 1)
        self.assertIn("/arbitral/doc/?arbitral-txt=", url)
        self.assertIn("page=2", build_search_url("q", "regular", 2))

    def test_unknown_section_raises(self):
        with self.assertRaises(ValueError):
            build_search_url("q", "bogus")



class QueryRelevanceGuardTests(unittest.TestCase):
    """SudAct silently returns its default list for non-matching
    queries; the parser must reject that (verified live 2026-09-10)."""

    def test_default_list_for_garbage_query_is_error(self):
        with self.assertRaises(EmptyPayloadError) as ctx:
            parse_search_html(HTML, limit=30, query="zzqxvбредовыйзапрос12345xyz",
                              section="arbitral", page=1,
                              source_url="u")
        self.assertIn("default list", str(ctx.exception))

    def test_matching_query_passes(self):
        res = parse_search_html(HTML, limit=30, query="банкротство физлиц",
                                section="arbitral", page=1,
                                source_url="u")
        self.assertEqual(res.to_dict()["count"], 30)

class JsRequiredTests(unittest.TestCase):
    """Verified live 2026-09-10: sudact search works only with JS;
    plain HTTP gets a stub; the old /?path silently ignores params."""

    def test_js_stub_raises(self):
        stub = ("<html><body>Поиск не доступен. У Вас отключен JavaScript"
                "</body></html>")
        with self.assertRaises(WafDetectedError):
            parse_search_html(stub, query="q", section="arbitral",
                              page=1, source_url="u")

    def test_js_mode_results_parse(self):
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-js-mode.html").read_text(
                    encoding="utf-8", errors="replace")
        res = parse_search_html(html, limit=30, query="поставка товаров",
                                section="arbitral", page=1,
                                source_url="u")
        d = res.to_dict()
        self.assertEqual(d["count"], 10)
        self.assertEqual(d["next_page"], 2)
        self.assertIn("Иркутской", d["results"][0]["title"])

    def test_build_url_uses_doc_endpoint(self):
        url = build_search_url("q", "arbitral", 1)
        self.assertIn("/arbitral/doc/?arbitral-txt=", url)


class FullTextTests(unittest.TestCase):
    """Full act text extraction for СОЮ decisions (agent needs the
    act body for legal-position analysis; snapshot stays structural)."""

    def test_extract_full_text_soyu(self):
        from case_search.sudact.parser import extract_full_text
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "regular-doc-2A-3400-2025.html").read_text(
                    encoding="utf-8", errors="replace")
        text = extract_full_text(html)
        self.assertGreater(len(text), 10000)
        self.assertIn("Р Е Ш Е Н И Е", text)
        self.assertIn("УСТАНОВИЛ:", text)
        # judge present in text
        self.assertIn("Шкарин", text)

    def test_extract_full_text_missing_container(self):
        from case_search.sudact.parser import extract_full_text
        self.assertEqual(extract_full_text("<html><body>x</body></html>"), "")

    def test_get_practice_doc_returns_full_text_in_extensions(self):
        out = get_practice_doc(
            "https://sudact.ru/regular/doc/V25ZOompTRg4/", None)
        # live test may be rate-limited; check shape when ok
        if out.ok:
            self.assertIn("full_text_chars", out.extensions)
            self.assertGreater(out.extensions["full_text_chars"], 5000)

if __name__ == "__main__":
    unittest.main()
