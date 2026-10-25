"""Audit 2026-09-10: tests for new infrastructure modules."""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class RateLimitTests(unittest.TestCase):
    def test_retry_succeeds_on_first_try(self):
        from case_search._rate_limit import retry_with_backoff
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        result = retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)

    def test_retry_on_rate_limit(self):
        from case_search._rate_limit import retry_with_backoff
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("HTTP 429 rate limit")
            return "ok"

        result = retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)

    def test_non_retryable_raises_immediately(self):
        from case_search._rate_limit import retry_with_backoff
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("not retryable")

        with self.assertRaises(ValueError):
            retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        self.assertEqual(len(calls), 1)

    def test_exhausted_retries_raises(self):
        from case_search._rate_limit import retry_with_backoff
        calls = []

        def fn():
            calls.append(1)
            raise OSError("429 too many")

        with self.assertRaises(OSError):
            retry_with_backoff(fn, max_retries=2, base_delay=0.01)
        self.assertEqual(len(calls), 2)

    def test_is_rate_limited(self):
        from case_search._rate_limit import is_rate_limited
        self.assertTrue(is_rate_limited(OSError("429 rate limit")))
        self.assertTrue(is_rate_limited(OSError("Too Many Requests")))
        self.assertFalse(is_rate_limited(OSError("connection refused")))
        self.assertFalse(is_rate_limited(ValueError("bad input")))

    def test_custom_retry_predicate(self):
        from case_search._rate_limit import retry_with_backoff
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("transient")
            return "done"

        result = retry_with_backoff(
            fn, max_retries=3, base_delay=0.01,
            retry_on=lambda e: isinstance(e, RuntimeError))
        self.assertEqual(result, "done")


class LoggingTests(unittest.TestCase):
    def test_get_logger_returns_child(self):
        from case_search._logging import get_logger
        logger = get_logger("test_module")
        self.assertEqual(logger.name, "case_search.test_module")

    def test_setup_logging_idempotent(self):
        from case_search._logging import setup_logging
        import io
        buf = io.StringIO()
        setup_logging(stream=buf)
        setup_logging(stream=buf)
        root = logging.getLogger("case_search")
        handler_count = sum(1 for h in root.handlers
                           if isinstance(h, logging.StreamHandler)
                           and h.stream is buf)
        self.assertEqual(handler_count, 1)

    def test_logger_writes_to_stream(self):
        import io
        buf = io.StringIO()
        logger = logging.getLogger("case_search.test_write")
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.info("audit-test-marker")
        output = buf.getvalue()
        self.assertIn("audit-test-marker", output)
        logger.removeHandler(handler)


class SearchIterTests(unittest.TestCase):
    def test_single_page_no_next(self):
        from case_search import search_iter
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-js-mode.html").read_text(
                    encoding="utf-8", errors="replace")

        call_count = [0]

        def fetcher(url):
            call_count[0] += 1
            if call_count[0] > 1:
                return "<html>no results here</html>"
            return html

        pages = list(search_iter("поставка товаров", fetcher=fetcher))
        self.assertTrue(pages[0].ok)
        self.assertEqual(call_count[0], 2)

    def test_stops_on_error(self):
        from case_search import search_iter

        def bad_fetcher(url):
            raise OSError("network down")

        pages = list(search_iter("test query", fetcher=bad_fetcher,
                                max_pages=5))
        self.assertEqual(len(pages), 1)
        self.assertFalse(pages[0].ok)

    def test_max_pages_cap(self):
        from case_search import search_iter
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-arbitral.html").read_text(
                    encoding="utf-8", errors="replace")

        def fetcher(url):
            return html

        pages = list(search_iter("банкротство физлиц", fetcher=fetcher,
                                max_pages=2))
        self.assertLessEqual(len(pages), 2)


class RelevanceGuardTests(unittest.TestCase):
    def test_stop_words_only_query_passes_without_guard(self):
        from case_search.sudact.search import parse_search_html
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-arbitral.html").read_text(
                    encoding="utf-8", errors="replace")
        res = parse_search_html(html, limit=30, query="в на по",
                                section="arbitral", page=1,
                                source_url="u")
        self.assertEqual(res.to_dict()["count"], 30)

    def test_short_meaningful_word_is_guarded(self):
        from case_search.sudact.search import parse_search_html
        from case_search.errors import EmptyPayloadError
        html = (REPO_ROOT / "fixtures" / "sudact"
                / "search-results-arbitral.html").read_text(
                    encoding="utf-8", errors="replace")
        with self.assertRaises(EmptyPayloadError):
            parse_search_html(html, limit=30, query="xyz",
                              section="arbitral", page=1,
                              source_url="u")


class VersionSyncTests(unittest.TestCase):
    def test_init_and_public_api_versions_match(self):
        import case_search
        from case_search import public_api
        self.assertEqual(case_search.__version__,
                        public_api.__version__)

    def test_version_is_semver(self):
        import case_search
        parts = case_search.__version__.split(".")
        self.assertGreaterEqual(len(parts), 3)


class CommonModuleTests(unittest.TestCase):
    def test_challenge_markers_shared(self):
        from case_search.sudact._common import CHALLENGE_MARKERS
        from case_search.sudact.parser import CHALLENGE_MARKERS as PM
        from case_search.sudact.search import CHALLENGE_MARKERS as SM
        self.assertIs(PM, CHALLENGE_MARKERS)
        self.assertIs(SM, CHALLENGE_MARKERS)

    def test_strip_tags_shared(self):
        from case_search.sudact._common import strip_tags
        result = strip_tags("<b>hello</b> &nbsp; world")
        self.assertEqual(result, "hello world")


class PyprojectTests(unittest.TestCase):
    def test_pyproject_exists_and_valid(self):
        import tomllib
        pp = REPO_ROOT / "pyproject.toml"
        self.assertTrue(pp.exists())
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        self.assertIn(data["project"]["name"], ("case-search", "sud-cli"))
        import case_search as _cw_mod
        import sud_cli as _sc_mod
        self.assertEqual(data["project"]["version"], _sc_mod.__version__)  # v2.3.2: список версий был brittle-захардкодом
        self.assertTrue(
            "case-search" in data["project"]["scripts"]
            or "sud-cli" in data["project"]["scripts"]
        )


if __name__ == "__main__":
    unittest.main()
