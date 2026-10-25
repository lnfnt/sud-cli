"""Tests for the injected live-browser adapter BOUNDARY (no network).

Every test drives `MosGorsudLiveSpikeProvider` through a fake browser
client; no code or test here performs I/O beyond reading local fixtures.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (EmptyPayloadError,  # noqa: E402
                                  HttpError,
                                  MalformedPayloadError,
                                  SourceUnavailableError,
                                  WafDetectedError)
from case_search.mos.live_spike import (APPROVED_CARDS,  # noqa: E402
                                          MAX_TIMEOUT_MS, BrowserPage,
                                          BrowserTimeoutError,
                                          MosGorsudLiveSpikeProvider)
from case_search.mos.parser import (classify_html,  # noqa: E402
                                      fetch_mos_html, parse_mos_html)
from case_search.pipeline import fetch  # noqa: E402
from case_search.sources import REGISTRY  # noqa: E402
from case_search.sources.base import (  # noqa: E402
    Provider, RawSourceResult)
from case_search.sources.live import MosGorsudProvider  # noqa: E402
from case_search.sources.mos_dom import (  # noqa: E402
    MosDomReplayProvider)

MOS_FIXTURES = REPO_ROOT / "mos-spike" / "fixtures"
HAPPY_REF = "02-0387/2025"
HAPPY_URL = APPROVED_CARDS[HAPPY_REF]
NOW = "2026-09-06T00:00:00Z"


def happy_html() -> str:
    return (MOS_FIXTURES / "case-02-0387-2025.redacted.html").read_text(
        encoding="utf-8")


def waf_html() -> str:
    return (MOS_FIXTURES / "waf.html").read_text(encoding="utf-8")


class FakeBrowser:
    """Injected boundary double: records calls, returns or raises."""

    def __init__(self, page=None, exc=None):
        self.page = page
        self.exc = exc
        self.calls: list[tuple[str, int]] = []

    def navigate(self, url: str, timeout_ms: int) -> BrowserPage:
        self.calls.append((url, timeout_ms))
        if self.exc is not None:
            raise self.exc
        assert self.page is not None, "fake browser misconfigured"
        return self.page


def ok_page(html=None, status=200, final_url=HAPPY_URL, **kw) -> BrowserPage:
    return BrowserPage(status=status, final_url=final_url,
                       html=happy_html() if html is None else html, **kw)


class AllowlistTests(unittest.TestCase):
    def test_only_approved_public_ref_is_mapped(self):
        self.assertEqual(list(APPROVED_CARDS), [HAPPY_REF])
        self.assertTrue(HAPPY_URL.startswith(
            "https://m.mos-gorsud.ru/rs/hamovnicheskij/services/cases/"
            "civil/details/"))

    def test_unknown_ref_fails_closed_without_navigating(self):
        browser = FakeBrowser(page=ok_page())
        provider = MosGorsudLiveSpikeProvider(browser)
        with self.assertRaises(SourceUnavailableError) as ctx:
            provider.lookup("02-9999/2099")
        self.assertIn("allowlist", str(ctx.exception))
        self.assertEqual(browser.calls, [])

    def test_synthetic_failure_refs_are_not_live_allowed(self):
        provider = MosGorsudLiveSpikeProvider(FakeBrowser(page=ok_page()))
        for ref in ("MOS-WAF", "MOS-EMPTY"):
            with self.assertRaises(SourceUnavailableError):
                provider.lookup(ref)


class ProviderContractTests(unittest.TestCase):
    def test_identity_flags(self):
        provider = MosGorsudLiveSpikeProvider(FakeBrowser())
        self.assertEqual(provider.name, "mos-gorsud-live-spike")
        self.assertEqual(provider.media_type, "text/html")
        self.assertFalse(provider.accepts_fixtures)

    def test_not_registered_under_production_names(self):
        self.assertNotIn("mos-gorsud-live-spike", REGISTRY)
        self.assertIs(REGISTRY["mos-gorsud"], MosGorsudProvider)
        with self.assertRaises(SourceUnavailableError):
            MosGorsudProvider().lookup(HAPPY_REF)

    def test_plain_callable_browser_accepted(self):
        calls = []

        def navigate(url, timeout_ms):
            calls.append((url, timeout_ms))
            return ok_page()

        raw = MosGorsudLiveSpikeProvider(navigate).lookup(HAPPY_REF)
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(calls, [(HAPPY_URL, 10000)])

    def test_bad_injected_browser_fails_closed(self):
        provider = MosGorsudLiveSpikeProvider(object())
        with self.assertRaises(SourceUnavailableError):
            provider.lookup(HAPPY_REF)

    def test_non_browser_page_result_rejected(self):
        provider = MosGorsudLiveSpikeProvider(
            lambda url, timeout_ms: "<html></html>")
        with self.assertRaises(MalformedPayloadError):
            provider.lookup(HAPPY_REF)


class NavigationBudgetTests(unittest.TestCase):
    def test_timeout_is_clamped_to_max(self):
        browser = FakeBrowser(page=ok_page())
        MosGorsudLiveSpikeProvider(browser, timeout_ms=60000).lookup(
            HAPPY_REF)
        self.assertEqual(browser.calls, [(HAPPY_URL, MAX_TIMEOUT_MS)])

    def test_default_timeout_is_bounded(self):
        browser = FakeBrowser(page=ok_page())
        MosGorsudLiveSpikeProvider(browser).lookup(HAPPY_REF)
        _url, timeout_ms = browser.calls[0]
        self.assertLessEqual(timeout_ms, MAX_TIMEOUT_MS)
        self.assertGreater(timeout_ms, 0)

    def test_exactly_one_navigation_no_retry(self):
        browser = FakeBrowser(page=ok_page())
        MosGorsudLiveSpikeProvider(browser).lookup(HAPPY_REF)
        self.assertEqual(len(browser.calls), 1)

    def test_timeout_failure_still_navigates_only_once(self):
        browser = FakeBrowser(exc=TimeoutError("hang"))
        provider = MosGorsudLiveSpikeProvider(browser)
        with self.assertRaises(BrowserTimeoutError):
            provider.lookup(HAPPY_REF)
        self.assertEqual(len(browser.calls), 1)

    def test_lookup_live_returns_raw_via_lookup(self):
        browser = FakeBrowser(page=ok_page())
        provider = MosGorsudLiveSpikeProvider(browser)
        raw = provider.lookup_live(HAPPY_REF, retrieved_at=NOW)
        self.assertIsInstance(raw, RawSourceResult)
        self.assertEqual(raw.media_type, "text/html")
        self.assertEqual(len(browser.calls), 1)

    def test_lookup_live_requires_retrieved_at(self):
        provider = MosGorsudLiveSpikeProvider(FakeBrowser(page=ok_page()))
        for stamp in ("", "   ", None):
            with self.assertRaises(ValueError):
                provider.lookup_live(HAPPY_REF, retrieved_at=stamp)


class SuccessPathTests(unittest.TestCase):
    def setUp(self):
        self.browser = FakeBrowser(page=ok_page())
        self.provider = MosGorsudLiveSpikeProvider(self.browser)
        self.raw = self.provider.lookup(HAPPY_REF)

    def test_raw_shape(self):
        self.assertEqual(self.raw.source,
                         f"mos-gorsud-live-spike:{HAPPY_REF}")
        self.assertEqual(self.raw.source_url, HAPPY_URL)
        self.assertEqual(self.raw.status_code, 200)
        self.assertEqual(self.raw.media_type, "text/html")
        self.assertIsNone(self.raw.error)
        self.assertTrue(self.raw.body.strip())

    def test_html_preserved_verbatim_and_routes_through_parser(self):
        self.assertEqual(self.raw.body, happy_html())
        record = parse_mos_html(self.raw.body, source=self.raw.source,
                                source_url=self.raw.source_url,
                                retrieved_at=NOW)
        self.assertEqual(record.case_number, HAPPY_REF)
        self.assertEqual(record.source_url, HAPPY_URL)

    def test_full_boundary_to_snapshot(self):
        outcome = fetch_mos_html(self.provider, HAPPY_REF, NOW)
        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(outcome.snapshot)
        self.assertEqual(outcome.snapshot.case_number, HAPPY_REF)
        self.assertIn("mos-gorsud-live-spike",
                      outcome.snapshot.provenance.source)
        self.assertEqual(outcome.snapshot.provenance.source_url, HAPPY_URL)

    def test_snapshot_carries_no_parties_tokens(self):
        outcome = fetch_mos_html(self.provider, HAPPY_REF, NOW)
        blob = outcome.snapshot.to_json() + outcome.snapshot.content_hash()
        for token in ("Истец", "Ответчик", "Стороны",
                      "законного представителя", "Долиной"):
            self.assertNotIn(token, blob)

    def test_live_and_replay_agree_on_case_fields(self):
        replay = MosDomReplayProvider(MOS_FIXTURES)
        live_snap = fetch_mos_html(self.provider, HAPPY_REF,
                                   NOW).snapshot.to_dict()
        replay_snap = fetch_mos_html(replay, HAPPY_REF,
                                     NOW).snapshot.to_dict()
        live_snap.pop("provenance")
        replay_snap.pop("provenance")
        self.assertEqual(live_snap, replay_snap)


class FailurePathTests(unittest.TestCase):
    def _run(self, provider):
        return fetch(provider, HAPPY_REF, NOW)

    def test_timeout_raises_and_never_yields_payload(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(exc=TimeoutError("navigation timed out")))
        with self.assertRaises(BrowserTimeoutError):
            provider.lookup(HAPPY_REF)
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("timeout", outcome.reason.casefold())

    def test_browser_timeout_error_propagates_unchanged(self):
        exc = BrowserTimeoutError("s", HAPPY_URL, 1000)
        provider = MosGorsudLiveSpikeProvider(FakeBrowser(exc=exc))
        with self.assertRaises(BrowserTimeoutError) as ctx:
            provider.lookup(HAPPY_REF)
        self.assertIs(ctx.exception, exc)

    def test_challenge_flag_is_degraded_not_empty_success(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(html="", challenge=True,
                                      retry_after=45)))
        raw = provider.lookup(HAPPY_REF)
        self.assertEqual(raw.error, "captcha_or_waf")
        self.assertEqual(raw.retry_after, 45)
        with self.assertRaises(WafDetectedError):
            classify_html(raw)
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "degraded")
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(outcome.retry_after, 45)

    def test_visible_interstitial_html_is_degraded(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(html=waf_html())))
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "degraded")
        self.assertIsNone(outcome.snapshot)

    def test_empty_html_is_error(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(html="   \n")))
        with self.assertRaises(EmptyPayloadError):
            provider.lookup(HAPPY_REF)
        self.assertEqual(self._run(provider).status, "error")

    def test_missing_card_marker_is_error_not_success(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(
                html="<html><body><h1>Сайт в обслуживании</h1>"
                     "<div>changed dom</div></body></html>")))
        with self.assertRaises(MalformedPayloadError) as ctx:
            provider.lookup(HAPPY_REF)
        self.assertIn("card marker", str(ctx.exception))
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)

    def test_marker_only_in_scripts_is_rejected(self):
        html = ('<html><body>'
                '<script>var t="Информация по делу № 02-0387/2025";'
                '</script><div>nothing visible</div></body></html>')
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(html=html)))
        with self.assertRaises(MalformedPayloadError):
            provider.lookup(HAPPY_REF)

    def test_non_200_status_is_http_error(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(status=500)))
        with self.assertRaises(HttpError):
            provider.lookup(HAPPY_REF)
        self.assertEqual(self._run(provider).status, "error")

    def test_not_found_is_http_error(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(status=404, html="")))
        with self.assertRaises(HttpError):
            provider.lookup(HAPPY_REF)

    def test_rate_limit_status_is_degraded(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(status=429, retry_after=30)))
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "degraded")
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(outcome.retry_after, 30)

    def test_redirect_off_allowlisted_url_is_error(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(
                final_url="https://m.mos-gorsud.ru/rs/hamovnicheskij/"
                          "search?q=02-0387/2025")))
        with self.assertRaises(MalformedPayloadError) as ctx:
            provider.lookup(HAPPY_REF)
        self.assertIn("unexpected final URL", str(ctx.exception))

    def test_foreign_host_final_url_is_error(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(
                final_url="https://evil.example/"
                          "rs/hamovnicheskij/services/cases/civil/"
                          "details/9ef63f11-5fa7-11ef-9d90-f3a74c864694")))
        with self.assertRaises(MalformedPayloadError):
            provider.lookup(HAPPY_REF)

    def test_trailing_slash_final_url_is_tolerated(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(final_url=HAPPY_URL + "/")))
        self.assertEqual(provider.lookup(HAPPY_REF).status_code, 200)


class AllowlistIntegrityTests(unittest.TestCase):
    def test_allowlist_is_read_only(self):
        def _assign():
            APPROVED_CARDS["02-9999/2099"] = "https://evil.example"

        def _delete():
            del APPROVED_CARDS[HAPPY_REF]

        def _update():
            APPROVED_CARDS.update({"X": "https://evil.example"})

        def _pop():
            APPROVED_CARDS.pop(HAPPY_REF)

        def _clear():
            APPROVED_CARDS.clear()

        for mutate in (_assign, _delete, _update, _pop, _clear):
            with self.assertRaises((TypeError, AttributeError)):
                mutate()
        self.assertEqual(list(APPROVED_CARDS), [HAPPY_REF])
        self.assertEqual(APPROVED_CARDS[HAPPY_REF], HAPPY_URL)


class IdentityBindingTests(unittest.TestCase):
    """Regression (found in review): a valid card of a DIFFERENT case
    served at the allowlisted URL must never become a successful lookup of
    the requested ref. The transport layer is intentionally agnostic; the
    shared fetch boundary binds parsed card identity to the case ref."""

    def _foreign_card(self) -> str:
        return happy_html().replace(HAPPY_REF, "02-9999/2025")

    def test_foreign_card_at_allowlisted_url_is_error_not_ok(self):
        provider = MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page(html=self._foreign_card())))
        raw = provider.lookup(HAPPY_REF)
        self.assertEqual(raw.status_code, 200)
        outcome = fetch_mos_html(provider, HAPPY_REF, NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("identity mismatch", outcome.reason)
        self.assertIn("02-9999/2025", outcome.reason)

    def test_binding_is_pipeline_wide_not_spike_specific(self):
        class StaticHtml(Provider):
            name = "static-html"
            media_type = "text/html"

            def __init__(self, body):
                self.body = body

            def lookup(self, case_ref):
                return RawSourceResult(
                    source="static-html", source_url="file://inline",
                    status_code=200, body=self.body,
                    media_type="text/html")

        outcome = fetch(StaticHtml(self._foreign_card()), HAPPY_REF, NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)


class BoundaryHygieneTests(unittest.TestCase):
    BANNED_MODULES = ("socket", "ssl", "http", "urllib", "ftplib",
                      "requests", "playwright", "selenium", "subprocess",
                      "browser_use")

    def _imported_modules(self):
        import ast
        import inspect

        import case_search.mos.live_spike as mod
        tree = ast.parse(inspect.getsource(mod))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods.add(node.module or "")
        return mods

    def test_no_network_or_browser_imports(self):
        for module in self._imported_modules():
            for banned in self.BANNED_MODULES:
                self.assertFalse(
                    module == banned or module.startswith(banned + "."),
                    f"forbidden import '{module}'")

    def test_no_filesystem_writes(self):
        import inspect

        import case_search.mos.live_spike as mod
        src = inspect.getsource(mod)
        for banned in ("open(", ".write(", ".mkdir", ".save", "shutil"):
            self.assertNotIn(banned, src)

    def test_pipeline_fetch_still_routes_by_media_type(self):
        outcome = fetch(MosGorsudLiveSpikeProvider(
            FakeBrowser(page=ok_page())), HAPPY_REF, NOW)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.snapshot.provenance.parser_version,
                         "mos-dom-replay-0.1.0")


if __name__ == "__main__":
    unittest.main()
