"""Tests for the transport-mode contract of the pravo.gov.ru adapter.

These cover the degraded-plaintext-HTTP mode, the explicit mode switch, and
the retry policy added after the the deployment host was observed to have no working TLS
path to publication.pravo.gov.ru while plain HTTP answered normally.

All tests are offline: they inject a fake transport or monkeypatch the opener.
No live network call is made.
"""

import socket
import urllib.request

import pytest

from sud_cli import pravo_publication as pp
from sud_cli.pravo_publication import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_TRANSPORT,
    MAX_TRANSPORT_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    TRANSPORT_ENV_VAR,
    PublicationError,
    resolve_transport_mode,
    source_url,
)

from tests.test_pravo_publication import FakeTransport, _json_bytes, _make_page


@pytest.fixture(autouse=True)
def _clean_transport_env(monkeypatch):
    """Never let the developer's own env leak into these assertions."""
    monkeypatch.delenv(TRANSPORT_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_defaults_to_http_when_nothing_is_configured():
    """Owner-approved default (2026-10-08): plain HTTP for this host."""
    assert resolve_transport_mode() == DEFAULT_TRANSPORT
    assert resolve_transport_mode() == "http"
    assert resolve_transport_mode(None) == "http"


def test_https_still_available_as_opt_in(monkeypatch):
    """Strict mode remains reachable for hosts with a working TLS path."""
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "https")
    assert resolve_transport_mode() == "https"


def test_env_var_selects_http_mode(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "http")
    assert resolve_transport_mode() == "http"


def test_explicit_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "http")
    assert resolve_transport_mode("https") == "https"


def test_unknown_mode_raises_instead_of_silently_defaulting(monkeypatch):
    """A typo must not silently degrade to plaintext or silently do nothing."""
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "htpp")
    with pytest.raises(PublicationError, match="unsupported transport mode"):
        resolve_transport_mode()


def test_source_url_reflects_active_mode():
    assert source_url("https") == f"https://{ALLOWED_HOST}{ALLOWED_PATH}"
    assert source_url("http") == f"http://{ALLOWED_HOST}{ALLOWED_PATH}"


# ---------------------------------------------------------------------------
# HTTP mode behaviour
# ---------------------------------------------------------------------------


def test_http_mode_queries_plaintext_endpoint():
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"http://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    pp.query_publication_metadata(query="widgets", transport=transport, scheme="http")

    url = transport.calls[0]["url"]
    assert url.startswith(f"http://{ALLOWED_HOST}{ALLOWED_PATH}?"), url


def test_http_mode_results_are_marked_unverified_and_carry_warning():
    """Plaintext metadata must never look authoritative."""
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"http://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    result = pp.query_publication_metadata(
        query="widgets", transport=transport, scheme="http"
    )

    assert result["integrity"] == "unverified_transport"
    assert result["transport_mode"] == "http"
    assert "warning" in result
    assert "plaintext" in result["warning"].lower()


def test_https_mode_results_are_marked_verified_without_warning():
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"https://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    result = pp.query_publication_metadata(
        query="widgets", transport=transport, scheme="https"
    )

    assert result["integrity"] == "tls_verified"
    assert result["transport_mode"] == "https"
    assert "warning" not in result


def test_item_provenance_matches_active_mode():
    page = _make_page([{"eoNumber": "EO-1", "title": "T"}])
    transport = FakeTransport(
        body=_json_bytes(page),
        url=f"http://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    result = pp.query_publication_metadata(
        query="widgets", transport=transport, scheme="http"
    )

    assert result["items"][0]["source"] == f"http://{ALLOWED_HOST}{ALLOWED_PATH}"


def test_http_mode_rejects_response_served_from_a_different_scheme():
    """An https response must not satisfy an http-mode request, and vice versa."""
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"https://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    with pytest.raises(PublicationError, match="http required"):
        pp.query_publication_metadata(
            query="widgets", transport=transport, scheme="http"
        )


def test_https_mode_still_rejects_plaintext_response():
    """The secure default must not accept a downgraded response."""
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"http://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    with pytest.raises(PublicationError, match="https required"):
        pp.query_publication_metadata(
            query="widgets", transport=transport, scheme="https"
        )


def test_no_silent_fallback_when_https_transport_fails(monkeypatch):
    """An https failure must raise; it must never retry itself over http."""
    calls = []

    def failing_opener(req, timeout=None):
        calls.append(req.full_url)
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(pp._opener, "open", failing_opener)
    monkeypatch.setattr(pp.time, "sleep", lambda _s: None)

    with pytest.raises(Exception):
        pp.query_publication_metadata(query="widgets", scheme="https")

    assert calls, "expected the transport to have been attempted"
    assert all(url.startswith("https://") for url in calls), calls


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_transport_retries_transient_dns_failures(monkeypatch):
    """The observed the project failure was intermittent DNS; retry must recover."""
    attempts = {"n": 0}

    class _Resp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def geturl(self):
            return f"http://{ALLOWED_HOST}{ALLOWED_PATH}?PageSize=10&Index=1&Name=widgets"

        def read(self, _n):
            return _json_bytes(_make_page([]))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def flaky_open(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise socket.gaierror("temporary failure in name resolution")
        return _Resp()

    monkeypatch.setattr(pp._opener, "open", flaky_open)
    monkeypatch.setattr(pp.time, "sleep", lambda _s: None)

    result = pp.query_publication_metadata(query="widgets", scheme="http")

    assert result["status"] == "ok"
    assert attempts["n"] == 3


def test_transport_gives_up_after_bounded_attempts(monkeypatch):
    attempts = {"n": 0}

    def always_fail(req, timeout=None):
        attempts["n"] += 1
        raise socket.gaierror("still failing")

    monkeypatch.setattr(pp._opener, "open", always_fail)
    monkeypatch.setattr(pp.time, "sleep", lambda _s: None)

    with pytest.raises(Exception):
        pp.query_publication_metadata(query="widgets", scheme="http")

    assert attempts["n"] == MAX_TRANSPORT_ATTEMPTS


def test_retry_backoff_is_bounded_and_short():
    """Retries must not become a hidden multi-second stall."""
    assert MAX_TRANSPORT_ATTEMPTS <= 5
    assert RETRY_BACKOFF_SECONDS <= 1.0


def test_redirect_is_still_refused_in_http_mode(monkeypatch):
    """Degrading the channel must not also relax the redirect rule."""

    class _RedirectingOpener:
        def open(self, req, timeout=None):
            raise pp.urllib.error.HTTPError(
                req.full_url, 302, "Found", {}, None
            )

    monkeypatch.setattr(pp, "_opener", _RedirectingOpener())

    with pytest.raises(PublicationError, match="redirect rejected"):
        pp.query_publication_metadata(query="widgets", scheme="http")


def test_request_headers_unchanged_in_http_mode():
    """No credentials may be attached just because the channel was relaxed."""
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"http://{ALLOWED_HOST}{ALLOWED_PATH}",
    )
    pp.query_publication_metadata(query="widgets", transport=transport, scheme="http")

    headers = transport.calls[0]["headers"]
    assert "Cookie" not in headers
    assert "Authorization" not in headers
    assert headers["User-Agent"] == pp.USER_AGENT