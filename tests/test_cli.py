# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

import json
import os

import pytest

from tests.conftest import run_cli


def test_help_prints_usage_and_exits_zero():
    code, out, err = run_cli(["--help"])
    assert code == 0
    assert "sud-cli" in out.lower() or "usage:" in out.lower()
    assert "providers" in out


def test_help_lists_law_search_command():
    code, out, err = run_cli(["--help"])
    assert code == 0
    assert "law" in out


def test_providers_lists_offline_available_and_live_unavailable():
    code, out, err = run_cli(["--json", "providers"])
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "ok"
    providers = {p["name"]: p for p in data["data"]}
    assert providers["local-json-court"]["status"] == "available"
    assert providers["local-json-npa"]["status"] == "available"
    assert providers["case-search-live"]["status"] == "available"
    assert providers["pravo-gov-ru"]["status"] == "available"
    assert providers["fees-calculator"]["status"] == "blocked"
    assert providers["deadlines-calculator"]["status"] == "blocked"


def test_providers_lists_kad_arbitr_ru_available():
    code, out, err = run_cli(["--json", "providers"])
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "ok"
    providers = {p["name"]: p for p in data["data"]}
    kad = providers["kad-arbitr-ru"]
    assert kad["kind"] == "live"
    assert kad["status"] == "available"
    desc = kad["description"].lower()
    assert "camofox" in desc
    assert "no live adapter is implemented" not in desc


def test_law_search_text_query_returns_metadata(monkeypatch, tmp_path):
    from sud_cli import pravo_publication

    page = {
        "items": [
            {
                "eoNumber": "EO-SYNTH-CLI-0001",
                "title": "Synthetic CLI Law",
                "name": "Synthetic CLI Law Name",
                "number": "SYNTH-CLI-0001",
                "documentDate": "2026-04-01",
                "publishDateShort": "2026-04-05",
            }
        ],
        "itemsTotalCount": 1,
        "currentPage": 1,
        "itemsPerPage": 10,
        "pagesTotalCount": 1,
    }
    calls = []

    def fake_transport(method, url, headers, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
        return pravo_publication.TransportResponse(
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps(page, ensure_ascii=False).encode("utf-8"),
            url=url,
        )

    monkeypatch.setattr(pravo_publication, "default_transport", fake_transport)

    code, out, err = run_cli(["--json", "law", "search", "widgets"])
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 1
    item = data["data"][0]
    assert item["eo_number"] == "EO-SYNTH-CLI-0001"
    assert item["publication_date"] == "2026-04-05"
    assert item["signed_date"] == "2026-04-01"
    assert "document_text" not in item
    assert len(calls) == 1
    assert "publication.pravo.gov.ru/api/Documents" in calls[0]["url"]
    assert "Name=widgets" in calls[0]["url"]


def test_law_search_number_query_returns_metadata(monkeypatch):
    from sud_cli import pravo_publication

    page = {
        "items": [
            {
                "eoNumber": "EO-SYNTH-NUM-0001",
                "title": "Synthetic Number Search Result",
                "number": "12345",
                "documentDate": "2026-05-01",
                "publishDateShort": "2026-05-05",
            }
        ],
        "itemsTotalCount": 1,
        "currentPage": 1,
        "itemsPerPage": 10,
        "pagesTotalCount": 1,
    }
    calls = []

    def fake_transport(method, url, headers, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
        return pravo_publication.TransportResponse(
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps(page, ensure_ascii=False).encode("utf-8"),
            url=url,
        )

    monkeypatch.setattr(pravo_publication, "default_transport", fake_transport)

    code, out, err = run_cli(["--json", "law", "search", "--number", "12345"])
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"][0]["document_number"] == "12345"
    assert len(calls) == 1
    assert "Number=12345" in calls[0]["url"]
    assert "NumberSearchType=0" in calls[0]["url"]
    assert "NumberSearchType=Equal" not in calls[0]["url"]


def test_law_search_transport_error_returns_non_zero_json_error(monkeypatch):
    from sud_cli import pravo_publication

    def failing_transport(method, url, headers, timeout):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(pravo_publication, "default_transport", failing_transport)

    code, out, err = run_cli(["--json", "law", "search", "widgets"])
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"
    assert "timeout" in data["data"].lower()


@pytest.mark.parametrize("args", [
    ["law", "search"],
    ["law", "search", "--number", "123", "text"],
])
def test_law_search_rejects_unsupported_query_shape(args):
    code, out, err = run_cli(["--json", *args])
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"


def test_law_search_timeout_error_does_not_expose_home_path(tmp_path, monkeypatch):
    """A provider timeout must produce a clear error without leaking the default
    state-directory path (which sits under $HOME).
    """
    from sud_cli import pravo_publication

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("SUD_CLI_STATE_DIR", raising=False)

    def failing_transport(method, url, headers, timeout):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(pravo_publication, "default_transport", failing_transport)

    # JSON mode: stderr must be a clear error without the fake home path.
    code, out, err = run_cli(["--json", "law", "search", "widgets"])
    assert code != 0
    assert out == ""
    data = json.loads(err)
    assert data["status"] == "error"
    assert "timeout" in data["data"].lower()
    combined = out + err
    assert str(fake_home) not in combined

    # Human mode: stderr must also stay free of the fake home path.
    code, out, err = run_cli(["law", "search", "widgets"])
    assert code != 0
    combined = out + err
    assert "ERROR:" in combined
    assert "timeout" in combined.lower()
    assert str(fake_home) not in combined
