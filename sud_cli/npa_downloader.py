# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Download full official NPA / court-act texts into local storage.

Fulfils the colleague rule (SOUL.md): a legal norm may not be cited unless its
full official text exists in local storage. This module downloads the official
document (PDF or plain text) by pravo.gov.ru publication number (``eoNumber``)
or by a direct URL to an official host, extracts the text layer, records
sha256 checksums, and stores everything together with a sidecar metadata file.

Fail-closed: only official hosts (pravo.gov.ru, vsrf.ru and their subdomains)
are accepted, redirects are rejected, path traversal in identifiers is
impossible (identifiers are strictly validated), and network/protocol failures
raise :class:`NpaDownloadError` instead of writing partial artifacts.

The HTTP transport is injectable (same pattern as
``sud_cli.pravo_publication``) so all automated tests stay offline.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from sud_cli.pravo_publication import (
    NoRedirectHandler,
    Transport,
    TransportResponse,
    resolve_transport_mode,
)


class NpaDownloadError(Exception):
    """Raised when an official document cannot be downloaded safely."""


PRAVO_PUBLICATION_HOST = "publication.pravo.gov.ru"
#: Official hosts a direct download URL may point to (exact host or subdomain).
ALLOWED_URL_SUFFIXES = ("pravo.gov.ru", "vsrf.ru")
TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
USER_AGENT = "sud-cli/0.1.0 (npa download; read-only)"
MAX_IDENTIFIER_LENGTH = 200
#: Identifiers become filenames: strict whitelist leaves no room for traversal.
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
EO_NUMBER_RE = re.compile(r"^\d{16}$")


def _utc_now_z() -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_transport(
    method: str, url: str, headers: dict, timeout: float
) -> TransportResponse:
    """Stdlib transport for the production path (tests inject fakes instead).

    Egress guard + anti-ban breaker are checked before any byte leaves the
    host; redirects are never followed (a redirect could move the request to a
    non-official host after the URL was validated).
    """
    from sud_cli.net_guard import BreakerOpenError, breaker_trip, check_before_fetch

    try:
        check_before_fetch(url)
    except BreakerOpenError as exc:
        raise NpaDownloadError(f"host in cooldown (anti-ban breaker): {exc}") from exc
    opener = urllib.request.build_opener(NoRedirectHandler)
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            # Read one byte past the cap so oversize bodies fail closed.
            body = resp.read(MAX_DOWNLOAD_BYTES + 1)
            return TransportResponse(
                status=resp.status,
                headers=dict(resp.headers),
                body=body,
                url=resp.geturl(),
            )
    except urllib.error.HTTPError as exc:
        # F-02 (аудит w2): 403/429 трипуют per-host breaker — хост уходит в
        # cooldown, последующие CLI-команды не долбят забаненный источник.
        if exc.code in (403, 429):
            breaker_trip(url, reason=f"http_{exc.code}")
        if 300 <= exc.code < 400:
            raise NpaDownloadError(f"redirect rejected (HTTP {exc.code})") from exc
        raise NpaDownloadError(f"HTTP {exc.code}") from exc
    except (socket.gaierror, TimeoutError, ConnectionError, OSError) as exc:
        raise NpaDownloadError(f"transport error: {exc}") from exc


def _validate_eo_number(eo_number: str) -> str:
    value = (eo_number or "").strip()
    if not EO_NUMBER_RE.match(value):
        raise NpaDownloadError(
            f"invalid eo_number {eo_number!r}: expected 16 digits "
            "(e.g. 0001202509240023)"
        )
    return value


def _validate_download_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise NpaDownloadError(f"unparseable url: {url!r}") from exc
    if parsed.scheme not in ("http", "https"):
        raise NpaDownloadError(f"scheme not allowed: {parsed.scheme!r} ({url!r})")
    if parsed.username is not None or parsed.password is not None:
        raise NpaDownloadError(f"credentialed url is not allowed: {url!r}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_URL_SUFFIXES):
        raise NpaDownloadError(
            f"host {host!r} is not an allowed official source "
            f"{ALLOWED_URL_SUFFIXES}"
        )
    return url


def _safe_identifier(identifier: str) -> str:
    """Identifiers become filenames; anything unsafe is rejected fail-closed."""
    if not SAFE_IDENTIFIER_RE.match(identifier) or ".." in identifier:
        raise NpaDownloadError(f"unsafe identifier derived: {identifier!r}")
    return identifier


def _identifier_for_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name.strip()
    # If path ends with generic endpoint like 'document', prefer hash/query param
    if name.lower() == "document" and parsed.query:
        q = urllib.parse.parse_qs(parsed.query)
        if "hash" in q:
            return f"doc_{q['hash'][0][:16]}"
        if "eoNumber" in q:
            return q["eoNumber"][0]
    if name:
        # Identifiers are extensionless stems: files are named {id}.pdf/.txt.
        stem = re.sub(r"\.(pdf|txt|html|htm)$", "", name, flags=re.IGNORECASE)
        if stem and SAFE_IDENTIFIER_RE.match(stem) and ".." not in stem:
            return stem
    # No usable basename: derive a stable, traversal-free identifier from URL.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_text(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Extract the text layer; graceful fallback instead of a hard failure.

    Returns (text, error). ``text`` is None when no text layer / OCR is
    available — the download itself still succeeds (the official PDF is the
    authoritative artifact).
    """
    try:
        from sud_cli.case_search.vsrf.parser import extract_pdf_text_info
        text, _info = extract_pdf_text_info(pdf_path)
        return text, None
    except Exception as exc:  # graceful: no text layer, no OCR backend, no pymupdf
        return None, str(exc)


def download_npa(
    *,
    eo_number: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "pravo",
    case_num: Optional[str] = None,
    out_dir: Optional[Path] = None,
    extract_text: bool = True,
    transport: Optional[Transport] = None,
) -> dict:
    """Download one official document into local NPA storage.

    Exactly one selector must be provided: ``eo_number`` (pravo.gov.ru
    publication number) or ``url`` (direct official URL). ``source=vsrf`` with
    only ``case_num`` fails closed: vsrf.ru has no stable case-number → file
    mapping, so a direct official URL is required.

    Returns the deterministic JSON envelope from the task brief.
    """
    if eo_number and url:
        raise NpaDownloadError("provide either eo_number or url, not both")
    if url:
        request_url = _validate_download_url(url)
        identifier = _safe_identifier(_identifier_for_url(request_url))
        source_label = urllib.parse.urlparse(request_url).hostname or "unknown"
        scheme = urllib.parse.urlparse(request_url).scheme
    elif eo_number:
        if source == "vsrf":
            raise NpaDownloadError("source=vsrf requires a direct vsrf.ru URL")
        eo = _validate_eo_number(eo_number)
        scheme = resolve_transport_mode()
        request_url = (
            f"{scheme}://{PRAVO_PUBLICATION_HOST}/file/pdf?eoNumber={eo}"
        )
        identifier = eo
        source_label = "pravo.gov.ru"
    elif source == "vsrf" and case_num:
        raise NpaDownloadError(
            "vsrf download by case number requires a direct document URL; "
            "pass the official vsrf.ru URL via --url"
        )
    else:
        raise NpaDownloadError("provide either eo_number or url")

    if out_dir is None:
        from sud_cli.config import get_state_dir
        out_dir = get_state_dir() / "npa"
    out_dir = Path(out_dir).expanduser()
    pdf_path = out_dir / f"{identifier}.pdf"
    txt_path = out_dir / f"{identifier}.txt"
    sidecar_path = out_dir / f"{identifier}.json"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,application/octet-stream;q=0.9,text/plain;q=0.8",
    }
    transport = transport or default_transport
    try:
        response = transport("GET", request_url, headers, TIMEOUT_SECONDS)
    except NpaDownloadError:
        raise
    except Exception as exc:
        raise NpaDownloadError(f"transport error: {exc}") from exc

    if response.status != 200:
        raise NpaDownloadError(f"HTTP {response.status}")
    body = response.body or b""
    if not body:
        raise NpaDownloadError("empty response body")
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise NpaDownloadError(
            f"response body exceeds maximum size of {MAX_DOWNLOAD_BYTES} bytes"
        )

    is_pdf = body.startswith(b"%PDF")
    content_type = response.headers.get("Content-Type", "")
    
    # publication.pravo.gov.ru file endpoint delivers strictly PDF files;
    # direct URLs can deliver PDF or consolidated text/html documents.
    if eo_number:
        if not is_pdf:
            raise NpaDownloadError(
                f"response is not a PDF document (content-type {content_type!r}, magic {body[:5]!r})"
            )
    else:
        # S2-4: HTML в URL-режиме — НЕ официальный текст: антибот/Qrator
        # отдаёт защитную страницу, которая раньше сохранялась как акт.
        # Официальные материалы — PDF или text/plain; всё остальное — отказ.
        is_text = content_type.startswith("text/plain")
        is_html = (content_type.startswith("text/html")
                   or body.startswith(b"<!DOC") or body.startswith(b"<html"))
        if is_html:
            raise NpaDownloadError(
                f"response is an HTML page, not an official document "
                f"(content-type {content_type!r}) — antibot/block page refused (S2-4)"
            )
        if not is_pdf and not is_text:
            raise NpaDownloadError(
                f"response is not a PDF or plain-text document "
                f"(content-type {content_type!r}, magic {body[:5]!r})"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded_at = _utc_now_z()

    pdf_sha: Optional[str] = None
    text_sha: Optional[str] = None
    text_chars = 0
    has_text_layer = False
    text_error: Optional[str] = None
    text_note: Optional[str] = None

    if is_pdf:
        pdf_sha = _sha256_bytes(body)
        if pdf_path.exists() and _sha256_file(pdf_path) == pdf_sha:
            status = "already_exists"
        else:
            pdf_path.write_bytes(body)
            status = "downloaded"
        if extract_text:
            if status == "already_exists" and txt_path.exists():
                stored = txt_path.read_text(encoding="utf-8", errors="replace")
                has_text_layer = True
                text_sha = _sha256_bytes(stored.encode("utf-8"))
                text_chars = len(stored)
            else:
                text, text_error = _extract_text(pdf_path)
                if text:
                    has_text_layer = True
                    txt_path.write_text(text, encoding="utf-8")
                    text_sha = _sha256_bytes(text.encode("utf-8"))
                    text_chars = len(text)
    else:
        # Plain-text or HTML official document: extract text if HTML, store as .txt
        status = "downloaded"
        try:
            raw_text = body.decode("utf-8", errors="replace")
        except Exception as exc:
            raise NpaDownloadError(f"document body decode error: {exc}") from exc
        
        if content_type.startswith("text/html") or body.startswith(b"<!DOC") or body.startswith(b"<html"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(raw_text, "html.parser")
                text = soup.get_text("\n", strip=True)
            except Exception:
                text = raw_text
        else:
            text = raw_text

        txt_path.write_text(text, encoding="utf-8")
        has_text_layer = True
        text_sha = _sha256_bytes(text.encode("utf-8"))
        text_chars = len(text)
        text_note = "stored as text (from official HTML/plain-text source)"

    sidecar = {
        "identifier": identifier,
        "source": source_label,
        "url": request_url,
        "pdf_sha256": pdf_sha,
        "text_sha256": text_sha,
        "size_bytes": len(body),
        "has_text_layer": has_text_layer,
        "downloaded_at": downloaded_at,
    }
    sidecar_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    document = {
        "source": source_label,
        "identifier": identifier,
        "pdf_path": str(pdf_path) if pdf_sha else None,
        "text_path": str(txt_path) if text_sha else None,
        "pdf_sha256": pdf_sha,
        "text_sha256": text_sha,
        "size_bytes": len(body),
        "text_char_count": text_chars,
        "has_text_layer": has_text_layer,
        "downloaded_at": downloaded_at,
    }
    if text_error:
        document["text_error"] = text_error
    if text_note:
        document["text_note"] = text_note
    return {
        "ok": True,
        "status": status,
        "document": document,
        "meta": {"command": "npa download", "transport": scheme},
    }
