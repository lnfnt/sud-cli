# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Tests for sud_cli.npa_downloader (TASK_NPA_DOWNLOAD).

All network access is faked via the injectable transport — tests stay offline.
"""

import hashlib
import json
import urllib.error

import pytest

from sud_cli import mcp_server, npa_downloader
from sud_cli.npa_downloader import NpaDownloadError, download_npa
from sud_cli.pravo_publication import TransportResponse

from .conftest import run_cli

EO = "0001202509240023"


class FakeTransport:
    """Recordable fake HTTP transport (same pattern as test_pravo_publication)."""

    def __init__(self, body=b"%PDF-1.4 fake", status=200,
                 content_type="application/pdf", side_effect=None):
        self.calls = []
        self.body = body
        self.status = status
        self.content_type = content_type
        self.side_effect = side_effect

    def __call__(self, method, url, headers, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers})
        if self.side_effect is not None:
            raise self.side_effect
        return TransportResponse(
            status=self.status,
            headers={"Content-Type": self.content_type},
            body=self.body,
            url=url,
        )


def _pdf_bytes(text="Hello NPA"):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


# --- download by eo_number ----------------------------------------------------

def test_download_by_eo_number(tmp_path):
    t = FakeTransport(body=b"%PDF-1.4 stub")
    payload = download_npa(eo_number=EO, out_dir=tmp_path, extract_text=False, transport=t)
    assert payload["ok"] is True
    assert payload["status"] == "downloaded"
    assert payload["meta"]["command"] == "npa download"
    doc = payload["document"]
    assert doc["identifier"] == EO
    assert doc["source"] == "pravo.gov.ru"
    assert doc["pdf_path"] == str(tmp_path / f"{EO}.pdf")
    assert doc["pdf_sha256"] == hashlib.sha256(b"%PDF-1.4 stub").hexdigest()
    assert doc["size_bytes"] == len(b"%PDF-1.4 stub")
    assert (tmp_path / f"{EO}.pdf").read_bytes() == b"%PDF-1.4 stub"
    sidecar = json.loads((tmp_path / f"{EO}.json").read_text(encoding="utf-8"))
    assert sidecar["pdf_sha256"] == doc["pdf_sha256"]
    assert sidecar["url"].endswith(f"/file/pdf?eoNumber={EO}")
    # default transport mode is http (owner-approved for publication.pravo.gov.ru)
    assert payload["meta"]["transport"] == "http"


def test_eo_number_url_targets_publication_host(tmp_path):
    t = FakeTransport()
    download_npa(eo_number=EO, out_dir=tmp_path, extract_text=False, transport=t)
    assert t.calls[0]["url"] == f"http://publication.pravo.gov.ru/file/pdf?eoNumber={EO}"


# --- text extraction -----------------------------------------------------------

def test_pdf_text_extraction_writes_txt_with_sha256(tmp_path):
    body = _pdf_bytes("Postanovlenie Plenuma VS RF")
    t = FakeTransport(body=body)
    payload = download_npa(eo_number=EO, out_dir=tmp_path, extract_text=True, transport=t)
    doc = payload["document"]
    assert doc["has_text_layer"] is True
    assert doc["text_path"] == str(tmp_path / f"{EO}.txt")
    assert doc["text_char_count"] > 0
    stored = (tmp_path / f"{EO}.txt").read_text(encoding="utf-8")
    assert "Postanovlenie Plenuma" in stored
    assert doc["text_sha256"] == hashlib.sha256(stored.encode("utf-8")).hexdigest()


def test_extraction_failure_is_graceful_not_fatal(tmp_path):
    # %PDF magic present, but the body is not a parseable PDF: download
    # succeeds, extraction degrades gracefully.
    t = FakeTransport(body=b"%PDF-1.4 not really a pdf")
    payload = download_npa(eo_number=EO, out_dir=tmp_path, extract_text=True, transport=t)
    doc = payload["document"]
    assert payload["ok"] is True
    assert doc["has_text_layer"] is False
    assert doc["text_path"] is None
    assert "text_error" in doc


# --- idempotency ----------------------------------------------------------------

def test_second_download_with_same_sha_is_already_exists(tmp_path):
    t = FakeTransport(body=b"%PDF-1.4 same-bytes")
    first = download_npa(eo_number=EO, out_dir=tmp_path, extract_text=False, transport=t)
    before = (tmp_path / f"{EO}.pdf").read_bytes()
    second = download_npa(eo_number=EO, out_dir=tmp_path, extract_text=False, transport=t)
    assert first["status"] == "downloaded"
    assert second["status"] == "already_exists"
    assert second["document"]["pdf_sha256"] == first["document"]["pdf_sha256"]
    # Файл не переписывается: на диске те же байты, что и после первой загрузки.
    assert (tmp_path / f"{EO}.pdf").read_bytes() == before


def test_changed_content_overwrites_and_reports_downloaded(tmp_path):
    t1 = FakeTransport(body=b"%PDF-1.4 v1")
    t2 = FakeTransport(body=b"%PDF-1.4 v2")
    download_npa(eo_number=EO, out_dir=tmp_path, extract_text=False, transport=t1)
    second = download_npa(eo_number=EO, out_dir=tmp_path, extract_text=False, transport=t2)
    assert second["status"] == "downloaded"
    assert (tmp_path / f"{EO}.pdf").read_bytes() == b"%PDF-1.4 v2"


# --- fail-closed ------------------------------------------------------------------

def test_http_404_fails_closed(tmp_path):
    t = FakeTransport(status=404)
    with pytest.raises(NpaDownloadError, match="HTTP 404"):
        download_npa(eo_number=EO, out_dir=tmp_path, transport=t)
    assert not (tmp_path / f"{EO}.pdf").exists()


def test_http_503_fails_closed(tmp_path):
    t = FakeTransport(status=503)
    with pytest.raises(NpaDownloadError, match="HTTP 503"):
        download_npa(eo_number=EO, out_dir=tmp_path, transport=t)


def test_network_error_fails_closed(tmp_path):
    t = FakeTransport(side_effect=ConnectionError("dns down"))
    with pytest.raises(NpaDownloadError, match="transport error"):
        download_npa(eo_number=EO, out_dir=tmp_path, transport=t)


def test_empty_body_fails_closed(tmp_path):
    t = FakeTransport(body=b"")
    with pytest.raises(NpaDownloadError, match="empty response body"):
        download_npa(eo_number=EO, out_dir=tmp_path, transport=t)


def test_non_pdf_response_fails_closed(tmp_path):
    t = FakeTransport(body=b"<html>blocked</html>", content_type="text/html")
    with pytest.raises(NpaDownloadError, match="not a PDF"):
        download_npa(eo_number=EO, out_dir=tmp_path, transport=t)


def test_invalid_eo_number_fails_closed(tmp_path):
    with pytest.raises(NpaDownloadError, match="invalid eo_number"):
        download_npa(eo_number="abc", out_dir=tmp_path, transport=FakeTransport())


def test_both_selectors_fail_closed(tmp_path):
    with pytest.raises(NpaDownloadError, match="not both"):
        download_npa(eo_number=EO, url="https://vsrf.ru/x.pdf", out_dir=tmp_path)


def test_no_selector_fails_closed(tmp_path):
    with pytest.raises(NpaDownloadError, match="eo_number or url"):
        download_npa(out_dir=tmp_path)


def test_disallowed_host_fails_closed(tmp_path):
    with pytest.raises(NpaDownloadError, match="not an allowed official source"):
        download_npa(url="https://example.com/doc.pdf", out_dir=tmp_path)


def test_vsrf_case_num_without_url_fails_closed(tmp_path):
    with pytest.raises(NpaDownloadError, match="direct document URL"):
        download_npa(source="vsrf", case_num="А40-217461/2024", out_dir=tmp_path)


def test_path_traversal_identifier_fails_closed(tmp_path):
    with pytest.raises(NpaDownloadError, match="unsafe identifier"):
        npa_downloader._safe_identifier("../../etc/passwd")
    with pytest.raises(NpaDownloadError, match="unsafe identifier"):
        npa_downloader._safe_identifier(".hidden")


# --- direct URL / plain text --------------------------------------------------------

def test_direct_url_plain_text_download(tmp_path):
    t = FakeTransport(body="Указ Президента РФ № 1".encode("utf-8"),
                      content_type="text/plain; charset=utf-8")
    payload = download_npa(url="https://www.vsrf.ru/acts/text.txt",
                           out_dir=tmp_path, extract_text=True, transport=t)
    doc = payload["document"]
    assert doc["source"] == "www.vsrf.ru"
    assert doc["pdf_path"] is None
    assert doc["has_text_layer"] is True
    stored = (tmp_path / "text.txt").read_text(encoding="utf-8")
    assert stored == "Указ Президента РФ № 1"
    assert doc["text_sha256"] == hashlib.sha256(stored.encode("utf-8")).hexdigest()


def test_direct_url_pdf_download(tmp_path):
    t = FakeTransport(body=b"%PDF-1.4 url-doc")
    payload = download_npa(url="https://publication.pravo.gov.ru/file/pdf?eoNumber=1",
                           out_dir=tmp_path, extract_text=False, transport=t)
    assert payload["document"]["pdf_sha256"] == hashlib.sha256(b"%PDF-1.4 url-doc").hexdigest()


# --- MCP tool ------------------------------------------------------------------------

def test_mcp_tool_download(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUD_CLI_LIVE_OK", "1")
    monkeypatch.setattr(npa_downloader, "default_transport",
                        FakeTransport(body=_pdf_bytes("MCP NPA text")))
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sud_cli_npa_download",
                   "arguments": {"eo_number": EO}}})
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True and body["status"] == "downloaded"
    assert (tmp_path / "npa" / f"{EO}.pdf").exists()


def test_mcp_tool_fail_closed_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUD_CLI_LIVE_OK", "1")
    monkeypatch.setattr(npa_downloader, "default_transport",
                        FakeTransport(status=503))
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "sud_cli_npa_download",
                   "arguments": {"eo_number": EO}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is False
    assert body["error"].startswith("DOWNLOAD_FAILED: HTTP 503")
    assert body["meta"]["command"] == "npa download"


def test_mcp_tool_schema_is_strict():
    tool = next(t for t in mcp_server.tool_list()["tools"]
                if t["name"] == "sud_cli_npa_download")
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"eo_number", "url", "source", "extract_text"}
    assert schema["properties"]["source"]["enum"] == ["pravo", "vsrf", "url"]


def test_mcp_tool_count_is_38():
    tools = mcp_server.tool_list()["tools"]
    assert len(tools) == 37  # sud_cli_update removed (S3-1)
    assert "sud_cli_npa_download" in {t["name"] for t in tools}


# --- CLI ------------------------------------------------------------------------------

def test_cli_npa_download_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_LIVE_OK", "1")
    monkeypatch.setattr(npa_downloader, "default_transport",
                        FakeTransport(body=_pdf_bytes("CLI NPA text")))
    code, out, err = run_cli([
        "npa", "download", "--eo-number", EO,
        "--out-dir", str(tmp_path / "npa"), "--json"])
    assert code == 0, err
    envelope = json.loads(out)
    assert envelope["status"] == "ok"
    doc = envelope["data"]["document"]
    assert doc["identifier"] == EO
    assert doc["has_text_layer"] is True
    assert (tmp_path / "npa" / f"{EO}.txt").exists()


def test_cli_npa_download_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_LIVE_OK", "1")
    monkeypatch.setattr(npa_downloader, "default_transport",
                        FakeTransport(status=404))
    code, out, err = run_cli([
        "npa", "download", "--eo-number", EO,
        "--out-dir", str(tmp_path / "npa"), "--json"])
    assert code == 1
    envelope = json.loads(err)
    assert envelope["status"] == "error"
    assert envelope["data"].startswith("DOWNLOAD_FAILED: HTTP 404")


def test_cli_npa_download_requires_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_LIVE_OK", "1")
    code, out, err = run_cli([
        "npa", "download", "--out-dir", str(tmp_path), "--json"])
    assert code == 1
    assert "DOWNLOAD_FAILED" in err
