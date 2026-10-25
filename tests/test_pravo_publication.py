"""Tests for the official publication.pravo.gov.ru metadata adapter."""

import email.message
import io
import json
import urllib.request
import urllib.response

import pytest

from sud_cli.pravo_publication import (
    ALLOWED_HOST,
    ALLOWED_PATH,
    DEFAULT_INDEX,
    DEFAULT_PAGE_SIZE,
    DEFAULT_TRANSPORT,
    MAX_QUERY_LENGTH,
    MAX_RESPONSE_BYTES,
    SCHEME,
    TIMEOUT_SECONDS,
    USER_AGENT,
    PublicationError,
    TransportResponse,
    query_publication_metadata,
)


class FakeTransport:
    """Recordable fake HTTP transport for offline adapter tests."""

    def __init__(self, response=None, body=None, status=200, headers=None, url=None, side_effect=None):
        self.calls = []
        self.response = response
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}
        self.url = url
        self.side_effect = side_effect

    def __call__(self, method, url, headers, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
        if self.side_effect:
            raise self.side_effect
        if self.response is not None:
            return self.response
        body = self.body if self.body is not None else b"{}"
        return TransportResponse(
            status=self.status,
            headers=self.headers,
            body=body,
            url=self.url or url,
        )


def _make_item(
    eo_number="EO-SYNTH-0001",
    title="Synthetic Public Law on Widgets",
    name="Synthetic Federal Law",
    number="SYNTH-0001",
    document_date="2026-01-15",
    publish_date="2026-01-20",
    document_type_id=1,
    authority_id=2,
):
    return {
        "eoNumber": eo_number,
        "title": title,
        "name": name,
        "number": number,
        "documentDate": document_date,
        "publishDateShort": publish_date,
        "documentTypeId": document_type_id,
        "authorityId": authority_id,
    }


def _make_page(items, total=None, current_page=1, items_per_page=DEFAULT_PAGE_SIZE, pages_total=1):
    return {
        "items": items,
        "itemsTotalCount": total if total is not None else len(items),
        "currentPage": current_page,
        "itemsPerPage": items_per_page,
        "pagesTotalCount": pages_total,
    }


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _assert_single_get_to_allowlisted_url(transport):
    assert len(transport.calls) == 1, "expected exactly one HTTP request"
    call = transport.calls[0]
    assert call["method"] == "GET"
    parsed = json.loads  # placeholder; we assert by string inspection below
    url = call["url"]
    assert url.startswith(f"{DEFAULT_TRANSPORT}://{ALLOWED_HOST}{ALLOWED_PATH}?"), url
    assert call["headers"].get("User-Agent") == USER_AGENT
    assert call["timeout"] == TIMEOUT_SECONDS
    return call


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_valid_text_query_returns_metadata_only():
    page = _make_page([_make_item()])
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(query="widgets", transport=transport)

    call = _assert_single_get_to_allowlisted_url(transport)
    assert "Name=widgets" in call["url"]
    assert f"PageSize={DEFAULT_PAGE_SIZE}" in call["url"]
    assert f"Index={DEFAULT_INDEX}" in call["url"]

    assert result["status"] == "ok"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["eo_number"] == "EO-SYNTH-0001"
    assert item["title"] == "Synthetic Public Law on Widgets"
    assert item["document_number"] == "SYNTH-0001"
    assert item["signed_date"] == "2026-01-15"
    assert item["publication_date"] == "2026-01-20"
    assert item["source"] == f"{DEFAULT_TRANSPORT}://{ALLOWED_HOST}{ALLOWED_PATH}"
    assert "retrieved_at" in item
    assert "document_text" not in item
    assert "body" not in item
    assert "name" not in item  # normalized to title only


def test_empty_results_return_metadata_with_zero_total():
    page = _make_page([], total=0, pages_total=0)
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(query="nomatch", transport=transport)

    assert result["status"] == "ok"
    assert result["items"] == []
    assert result["pagination"]["total"] == 0
    assert result["pagination"]["page"] == DEFAULT_INDEX
    assert result["pagination"]["page_size"] == DEFAULT_PAGE_SIZE


def test_exact_number_query_uses_numeric_search_type_zero():
    """Official API help documents NumberSearchType: 0=exact, 1=starts-with,
    2=ends-with, 3=contains. Exact number search must send 0, not the
    unsupported string ``Equal``.
    """
    page = _make_page([_make_item(number="123456")])
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(number="123456", transport=transport)

    call = _assert_single_get_to_allowlisted_url(transport)
    assert "Number=123456" in call["url"]
    assert "NumberSearchType=0" in call["url"]
    assert "NumberSearchType=Equal" not in call["url"]
    assert "Name=" not in call["url"]
    assert result["status"] == "ok"
    assert result["items"][0]["document_number"] == "123456"


def test_pagination_parameters_are_fixed_to_defaults():
    page = _make_page([], total=0, pages_total=0)
    transport = FakeTransport(body=_json_bytes(page))

    query_publication_metadata(query="anything", transport=transport)

    call = _assert_single_get_to_allowlisted_url(transport)
    assert f"PageSize={DEFAULT_PAGE_SIZE}" in call["url"]
    assert f"Index={DEFAULT_INDEX}" in call["url"]


def test_metadata_field_normalization_handles_nulls_and_missing_fields():
    page = _make_page(
        [
            {
                "eoNumber": None,
                "title": None,
                "name": "Fallback Name",
                "number": "SYNTH-NULL",
                "documentDate": None,
                "publishDateShort": "2026-02-01",
            }
        ]
    )
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(query="null", transport=transport)

    item = result["items"][0]
    assert item["eo_number"] is None
    assert item["title"] == "Fallback Name"
    assert item["signed_date"] is None
    assert item["publication_date"] == "2026-02-01"


def test_dates_are_preserved_distinctly_not_conflated():
    page = _make_page([_make_item(document_date="2026-03-01", publish_date="2026-03-10")])
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(query="dates", transport=transport)

    item = result["items"][0]
    assert item["signed_date"] == "2026-03-01"
    assert item["publication_date"] == "2026-03-10"
    assert item["signed_date"] != item["publication_date"]


def test_source_provenance_and_provider_label_are_explicit():
    page = _make_page([_make_item()])
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(query="widgets", transport=transport)

    assert result["source"] == f"{DEFAULT_TRANSPORT}://{ALLOWED_HOST}{ALLOWED_PATH}"
    assert "official-publication metadata only" in result["provider_label"].lower()
    assert "not consolidated/current text" in result["provider_label"].lower()


# ---------------------------------------------------------------------------
# Fail-closed tests
# ---------------------------------------------------------------------------


def test_timeout_raises_explicit_publication_error():
    transport = FakeTransport(side_effect=TimeoutError("connection timed out"))

    with pytest.raises(PublicationError, match="timeout"):
        query_publication_metadata(query="widgets", transport=transport)


def test_http_error_raises_explicit_publication_error():
    transport = FakeTransport(status=500, body=b"Internal Server Error")

    with pytest.raises(PublicationError, match="HTTP 500"):
        query_publication_metadata(query="widgets", transport=transport)


def test_cross_host_redirect_is_rejected():
    # Same scheme as the active transport, but a foreign host: the host check
    # must reject it regardless of scheme.
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url=f"{DEFAULT_TRANSPORT}://evil.example.com/api/Documents",
    )

    with pytest.raises(PublicationError, match="redirect|host"):
        query_publication_metadata(query="widgets", transport=transport)


def test_wrong_content_type_is_rejected():
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

    with pytest.raises(PublicationError, match="content.type"):
        query_publication_metadata(query="widgets", transport=transport)


def test_oversize_payload_is_rejected():
    huge = b"x" * (MAX_RESPONSE_BYTES + 1)
    transport = FakeTransport(body=huge)

    with pytest.raises(PublicationError, match="size|oversize|large"):
        query_publication_metadata(query="widgets", transport=transport)


def test_malformed_json_raises_explicit_publication_error():
    transport = FakeTransport(body=b"not valid json")

    with pytest.raises(PublicationError, match="json|parse"):
        query_publication_metadata(query="widgets", transport=transport)


def test_unexpected_schema_raises_explicit_publication_error():
    transport = FakeTransport(body=_json_bytes({"unexpected": "payload"}))

    with pytest.raises(PublicationError, match="schema|unexpected"):
        query_publication_metadata(query="widgets", transport=transport)


def test_result_does_not_include_full_text_or_body():
    page = _make_page(
        [
            {
                **_make_item(),
                "documentText": "This full text must not leak into output.",
                "body": "This body must not leak either.",
            }
        ]
    )
    transport = FakeTransport(body=_json_bytes(page))

    result = query_publication_metadata(query="widgets", transport=transport)

    payload = json.dumps(result)
    assert "full text must not leak" not in payload
    assert "body must not leak" not in payload


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------


def test_rejects_both_query_and_number():
    transport = FakeTransport(body=_json_bytes(_make_page([])))

    with pytest.raises(PublicationError, match="query.*number|number.*query"):
        query_publication_metadata(query="widgets", number="123", transport=transport)
    assert transport.calls == []


def test_rejects_missing_query_and_number():
    transport = FakeTransport(body=_json_bytes(_make_page([])))

    with pytest.raises(PublicationError, match="query.*number|number.*query"):
        query_publication_metadata(transport=transport)
    assert transport.calls == []


def test_rejects_query_exceeding_max_length():
    transport = FakeTransport(body=_json_bytes(_make_page([])))

    with pytest.raises(PublicationError, match="length"):
        query_publication_metadata(query="x" * (MAX_QUERY_LENGTH + 1), transport=transport)
    assert transport.calls == []


def test_rejects_empty_query_and_empty_number():
    transport = FakeTransport(body=_json_bytes(_make_page([])))

    with pytest.raises(PublicationError, match="query.*number|number.*query|empty"):
        query_publication_metadata(query="", transport=transport)
    with pytest.raises(PublicationError, match="query.*number|number.*query|empty"):
        query_publication_metadata(number="", transport=transport)
    assert transport.calls == []


def test_rejects_url_on_a_different_scheme_than_active_transport():
    """A response served over the non-active scheme must be rejected.

    With the owner-approved ``http`` default, an ``https`` response is the
    mismatched one. This keeps the original scheme-confusion check meaningful
    after the default changed.
    """
    transport = FakeTransport(
        body=_json_bytes(_make_page([])),
        url="https://publication.pravo.gov.ru/api/Documents",
    )

    with pytest.raises(PublicationError, match="http required|scheme"):
        query_publication_metadata(query="widgets", transport=transport)


# ---------------------------------------------------------------------------
# Provider registry / CLI integration tests
# ---------------------------------------------------------------------------


def test_provider_registry_marks_pravo_publication_available():
    from sud_cli.providers import REGISTRY

    by_name = {p.name: p for p in REGISTRY}
    assert "pravo-gov-ru" in by_name
    assert by_name["pravo-gov-ru"].status == "available"
    assert "metadata" in by_name["pravo-gov-ru"].description.lower()


# ---------------------------------------------------------------------------
# Security / boundary tests
# ---------------------------------------------------------------------------


def test_request_has_no_cookie_or_auth_header():
    page = _make_page([])
    transport = FakeTransport(body=_json_bytes(page))

    query_publication_metadata(query="widgets", transport=transport)

    headers = transport.calls[0]["headers"]
    assert "Cookie" not in headers
    assert "Authorization" not in headers


class _FakeFile(io.BytesIO):
    """BytesIO stand-in that exposes the ``msg`` attribute urllib expects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.msg = email.message.Message()


def test_default_transport_refuses_redirect_without_following(monkeypatch):
    """urllib.request follows redirects by default. The production transport must
    fail closed on any 3xx instead of making a second request to a different host.
    """
    from sud_cli.pravo_publication import NoRedirectHandler, default_transport

    calls = []

    class RecordingHandler(urllib.request.HTTPHandler):
        """Records both http:// and https:// open() calls.

        The handler is scheme-agnostic because the owner-approved default is
        plain HTTP; a HTTPS-only handler would silently stop intercepting
        requests and the test would pass vacuously.
        """

        def http_open(self, req):
            calls.append(req.full_url)
            body = _FakeFile(b"")
            headers = email.message.Message()
            headers["Location"] = "https://evil.example.com/api/Documents"
            return urllib.response.addinfourl(body, headers, req.full_url, code=302)

        https_open = http_open

    test_opener = urllib.request.build_opener(RecordingHandler, NoRedirectHandler)
    monkeypatch.setattr("sud_cli.pravo_publication._opener", test_opener)

    with pytest.raises(PublicationError, match="redirect"):
        default_transport(
            "GET",
            f"{DEFAULT_TRANSPORT}://{ALLOWED_HOST}{ALLOWED_PATH}?Name=x",
            {},
            TIMEOUT_SECONDS,
        )

    assert len(calls) == 1, "redirect must not trigger an additional request"


def test_default_transport_non_redirect_response_stays_fine(monkeypatch):
    from sud_cli.pravo_publication import NoRedirectHandler, default_transport

    calls = []
    payload = _json_bytes(_make_page([]))

    class RecordingHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            calls.append(req.full_url)
            body = _FakeFile(payload)
            headers = email.message.Message()
            headers["Content-Type"] = "application/json; charset=utf-8"
            return urllib.response.addinfourl(body, headers, req.full_url, code=200)

        https_open = http_open

    test_opener = urllib.request.build_opener(RecordingHandler, NoRedirectHandler)
    monkeypatch.setattr("sud_cli.pravo_publication._opener", test_opener)

    response = default_transport(
        "GET",
        f"{DEFAULT_TRANSPORT}://{ALLOWED_HOST}{ALLOWED_PATH}?Name=x",
        {},
        TIMEOUT_SECONDS,
    )

    assert response.status == 200
    assert response.body == payload
    assert len(calls) == 1


def test_default_transport_intercepts_both_schemes(monkeypatch):
    """Guard against a future scheme change making redirect tests vacuous.

    If DEFAULT_TRANSPORT ever flips back to https, the handler-agnostic
    redirect tests above must still intercept. This drives ``default_transport``
    with a recording opener for each scheme, entirely offline — it never
    performs a real network request.
    """
    from sud_cli.pravo_publication import NoRedirectHandler, default_transport

    payload = _json_bytes(_make_page([]))

    for scheme in ("http", "https"):

        class RecordingHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
            """Subclasses both handler classes so build_opener registers it for
            either scheme; a single-scheme handler would let the other scheme
            fall through to a real network connection."""

            def http_open(self, req):
                seen.append(req.full_url)
                body = _FakeFile(payload)
                headers = email.message.Message()
                headers["Content-Type"] = "application/json; charset=utf-8"
                return urllib.response.addinfourl(body, headers, req.full_url, code=200)

            https_open = http_open

        seen = []
        opener = urllib.request.build_opener(RecordingHandler(), NoRedirectHandler)
        monkeypatch.setattr("sud_cli.pravo_publication._opener", opener)

        response = default_transport(
            "GET",
            f"{scheme}://{ALLOWED_HOST}{ALLOWED_PATH}?Name=x",
            {},
            TIMEOUT_SECONDS,
        )

        assert seen, f"{scheme} request was not intercepted by the test handler"
        assert response.status == 200
        assert response.body == payload
