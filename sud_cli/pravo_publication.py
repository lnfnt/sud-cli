"""Official publication.pravo.gov.ru metadata adapter.

This module performs a single, bounded, read-only GET to the documented
``/api/Documents`` endpoint on ``publication.pravo.gov.ru``. It returns only
publication metadata and makes no claims about consolidated/current law text or
legal status.

Transport modes
---------------
``http`` (default)
    Plain HTTP to ``publication.pravo.gov.ru``. This is the owner-approved
    default for this host (decision of 2026-10-08): TLS to
    publication.pravo.gov.ru is not reachable from the the deployment host, while plain
    HTTP answers normally. Plaintext cannot be protected against a network-path
    MITM, so results obtained this way are marked
    ``integrity: unverified_transport`` and must not be treated as
    authoritative on their own.

``https``
    Opt-in strict mode. Use it when a working TLS path exists: it is the only
    mode that protects metadata integrity end to end.

The mode is chosen by the caller or by ``SUD_CLI_PRAVO_TRANSPORT``. An unknown
value raises rather than falling back to a default, so a typo cannot silently
select a channel.

There is still no silent HTTPS-to-HTTP fallback *within* a single invocation:
a transport failure in ``https`` mode raises instead of quietly degrading. The
owner-approved default is applied once, at resolution time, and is visible in
the result as ``transport_mode``.
"""

import datetime
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


class PublicationError(Exception):
    """Raised when a publication query cannot be completed safely."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on any HTTP redirect rather than following it.

    urllib.request follows redirects automatically, which could make an
    additional request to a different host before the final URL is validated.
    This handler rejects every 3xx before a second request is issued.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect rejected", headers, fp
        )


@dataclass(frozen=True)
class TransportResponse:
    """Minimal HTTP response shape used by the adapter."""

    status: int
    headers: Dict[str, str]
    body: bytes
    url: str


Transport = Callable[[str, str, Dict[str, str], float], TransportResponse]

ALLOWED_HOST = "publication.pravo.gov.ru"
ALLOWED_PATH = "/api/Documents"
#: Retained for backwards compatibility: the strict TLS scheme. Callers should
#: use ``resolve_transport_mode()`` instead of assuming this is the active mode.
SCHEME = "https"
TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
USER_AGENT = "sud-cli/0.1.0 (metadata query; read-only)"
MAX_QUERY_LENGTH = 200
DEFAULT_PAGE_SIZE = 10
DEFAULT_INDEX = 1

#: Transport mode switch. ``http`` is the owner-approved default for this host;
#: ``https`` is opt-in strict mode.
TRANSPORT_ENV_VAR = "SUD_CLI_PRAVO_TRANSPORT"
SUPPORTED_TRANSPORT_MODES = ("https", "http")

#: Owner-approved default (decision 2026-10-08): TLS is unreachable to
#: publication.pravo.gov.ru from the the deployment host, and plain HTTP is acceptable for
#: public legal-publication metadata.
DEFAULT_TRANSPORT = "http"

#: Integrity label attached to results, per transport mode.
_INTEGRITY_BY_MODE = {
    "https": "tls_verified",
    "http": "unverified_transport",
}

#: Retry policy for transient failures. The observed failure on the the deployment host
#: was intermittent DNS resolution (gaierror) plus TLS connect timeouts, so a
#: single attempt is not a reliable signal. Retries are bounded and total
#: wall-clock time stays small.
MAX_TRANSPORT_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.4


def resolve_transport_mode(override: Optional[str] = None) -> str:
    """Resolve the active transport mode.

    Precedence: explicit argument, then ``SUD_CLI_PRAVO_TRANSPORT``, then the
    owner-approved ``http`` default. An unknown value raises rather than
    falling back, so a typo can neither silently downgrade nor silently
    ignore the setting.
    """
    mode = override
    if mode is None:
        mode = os.environ.get(TRANSPORT_ENV_VAR)
    if mode is None:
        return DEFAULT_TRANSPORT
    mode = mode.strip().lower()
    if mode not in SUPPORTED_TRANSPORT_MODES:
        raise PublicationError(
            f"unsupported transport mode {mode!r}; "
            f"expected one of {SUPPORTED_TRANSPORT_MODES}"
        )
    return mode


def source_url(mode: Optional[str] = None) -> str:
    """Canonical provenance string for the active transport mode."""
    active = resolve_transport_mode(mode)
    return f"{active}://{ALLOWED_HOST}{ALLOWED_PATH}"


def _utc_now() -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc).replace(microsecond=0).isoformat()
    )


# Module-level opener with redirect handling disabled. Using a shared opener
# avoids the default urllib.request.urlopen behavior of silently following
# cross-host redirects and exposing query data to an intermediate request.
_opener = urllib.request.build_opener(NoRedirectHandler)


def default_transport(
    method: str, url: str, headers: Dict[str, str], timeout: float
) -> TransportResponse:
    """Standard-library transport for the production path.

    Retries bounded transient failures (DNS resolution errors, TLS connect
    timeouts, connection resets) because the host intermittently fails to
    resolve. Redirects are never followed, in either scheme: a redirect could
    move the request to another host before the URL is validated.

    Tests never call this function directly; they inject a fake transport.
    """
    last_error: Optional[BaseException] = None
    # v0.1.1: egress-guard + breaker (pravo.gov.ru — внешний официальный источник)
    from sud_cli.sud_netguard import BreakerOpenError, check_before_fetch

    try:
        check_before_fetch(url)
    except BreakerOpenError as exc:
        raise PublicationError(f"pravo host in cooldown (anti-ban breaker): {exc}") from exc
    for attempt in range(MAX_TRANSPORT_ATTEMPTS):
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=timeout) as resp:
                # Read one byte past the cap so we can fail-closed on oversize bodies.
                body = resp.read(MAX_RESPONSE_BYTES + 1)
                return TransportResponse(
                    status=resp.status,
                    headers=dict(resp.headers),
                    body=body,
                    url=resp.geturl(),
                )
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise PublicationError(
                    f"redirect rejected (HTTP {exc.code})"
                ) from exc
            raise
        except (socket.gaierror, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
    # Unreachable in practice: the loop either returns or raises.
    raise PublicationError(f"transport error: {last_error}")


def _build_url(query: Optional[str], number: Optional[str], scheme: str = DEFAULT_TRANSPORT) -> str:
    params = [
        ("PageSize", str(DEFAULT_PAGE_SIZE)),
        ("Index", str(DEFAULT_INDEX)),
    ]
    if query is not None:
        params.append(("Name", query))
    if number is not None:
        params.append(("Number", number))
        params.append(("NumberSearchType", "0"))
    query_string = urllib.parse.urlencode(params)
    return f"{scheme}://{ALLOWED_HOST}{ALLOWED_PATH}?{query_string}"


def _validate_url(url: str, scheme: str = DEFAULT_TRANSPORT) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != scheme:
        raise PublicationError(
            f"{scheme} required; got scheme {parsed.scheme!r}"
        )
    if parsed.netloc.lower() != ALLOWED_HOST.lower():
        raise PublicationError(f"unexpected host {parsed.netloc!r}")
    if parsed.path != ALLOWED_PATH:
        raise PublicationError(f"unexpected path {parsed.path!r}")


def _validate_response(response: TransportResponse, scheme: str = DEFAULT_TRANSPORT) -> Any:
    _validate_url(response.url, scheme)
    if response.status != 200:
        raise PublicationError(f"HTTP {response.status}")
    content_type = response.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        raise PublicationError(f"unexpected content type {content_type!r}")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise PublicationError(
            f"response body exceeds maximum size of {MAX_RESPONSE_BYTES} bytes"
        )
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"malformed json response: {exc}") from exc
    if not isinstance(payload, dict):
        raise PublicationError("response root must be a JSON object")
    required = (
        "items",
        "itemsTotalCount",
        "currentPage",
        "itemsPerPage",
        "pagesTotalCount",
    )
    for key in required:
        if key not in payload:
            raise PublicationError(f"response schema error: missing required field {key!r}")
    if not isinstance(payload["items"], list):
        raise PublicationError("'items' must be an array")
    return payload


def _normalize_item(item: Any, scheme: str = DEFAULT_TRANSPORT) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise PublicationError("each item must be a JSON object")
    eo_number = item.get("eoNumber")
    title = item.get("title") or item.get("name")
    number = item.get("number")
    document_date = item.get("documentDate")
    publish_date = item.get("publishDateShort")
    return {
        "eo_number": eo_number,
        "title": title,
        "document_number": number,
        "signed_date": document_date,
        "publication_date": publish_date,
        "source": source_url(scheme),
        "retrieved_at": _utc_now(),
    }


def query_publication_metadata(
    query: Optional[str] = None,
    number: Optional[str] = None,
    transport: Optional[Transport] = None,
    scheme: Optional[str] = None,
) -> Dict[str, Any]:
    """Query the official publication endpoint for metadata only.

    Exactly one HTTP GET is performed per invocation (the transport may retry
    a bounded number of times on transient network failures). The transport
    callable is injectable so all automated tests remain offline.

    ``scheme`` selects the transport mode. When omitted it is resolved from
    ``SUD_CLI_PRAVO_TRANSPORT`` and defaults to :data:`DEFAULT_TRANSPORT`
    (``http`` — plain HTTP is owner-approved for this host; see the module
    docstring). Results fetched over ``http`` carry
    ``integrity: unverified_transport``.

    Raises:
        PublicationError: on invalid input, transport failure, non-200 HTTP,
            cross-host redirect, content-type mismatch, oversize payload,
            malformed JSON, or unexpected response schema.
    """
    active_scheme = resolve_transport_mode(scheme)
    if query is not None and number is not None:
        raise PublicationError("provide either query or number, not both")
    if query is None and number is None:
        raise PublicationError("provide a text query or an exact document number")
    active = query if query is not None else number
    if active == "":
        raise PublicationError("query/number must not be empty")
    if len(active) > MAX_QUERY_LENGTH:
        raise PublicationError(
            f"search term exceeds maximum length of {MAX_QUERY_LENGTH}"
        )

    transport = transport or default_transport
    url = _build_url(query, number, active_scheme)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    try:
        response = transport("GET", url, headers, TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise PublicationError(f"request timeout after {TIMEOUT_SECONDS}s") from exc
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise PublicationError(
                f"redirect rejected (HTTP {exc.code})"
            ) from exc
        raise PublicationError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise PublicationError(f"transport error: {exc.reason}") from exc
    except PublicationError:
        raise
    except Exception as exc:
        raise PublicationError(f"transport error: {exc}") from exc

    payload = _validate_response(response, active_scheme)
    items = [_normalize_item(item, active_scheme) for item in payload["items"]]
    integrity = _INTEGRITY_BY_MODE[active_scheme]
    result = {
        "status": "ok",
        "items": items,
        "pagination": {
            "page": payload["currentPage"],
            "page_size": payload["itemsPerPage"],
            "total": payload["itemsTotalCount"],
            "pages_total": payload["pagesTotalCount"],
        },
        "source": source_url(active_scheme),
        "transport_mode": active_scheme,
        "integrity": integrity,
        "provider_label": (
            "official-publication metadata only "
            "(not consolidated/current text or legal status)"
        ),
    }
    if integrity != "tls_verified":
        # Never let plaintext results masquerade as authoritative metadata.
        result["warning"] = (
            "metadata fetched over plaintext HTTP: transport integrity is "
            "unverified, verify independently before relying on it"
        )
    return result
