# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Live commands are core functionality (owner decision 07.11.2026).

The ``SUD_CLI_LIVE_OK`` env gate is gone: practice/kad/case-history/
extract-text/npa-download run by default, with the network still mocked or
fail-closed by fact (typed error naming the cause). These tests pin the
default-enabled contract — no env variable may be required for dispatch.
"""

import json

import pytest

from tests.conftest import run_cli


class _Outcome:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


@pytest.mark.parametrize("argv", [
    ["--json", "practice", "запрос"],
    ["--json", "kad", "--name", "Иванов"],
    ["--json", "case-history", "А40-1/2026"],
    ["--json", "extract-text", "/tmp/x.html"],
])
def test_live_command_runs_without_env(tmp_path, monkeypatch, argv):
    """Default-enabled: the command reaches the native API without any env opt-in."""
    import sud_cli.case_search.public_api as public_api

    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)
    seen = {}

    def fake_practice(query=None, **kw):
        seen["query"] = query
        return _Outcome({"status": "ok"})

    def fake_kad(inn=None, ogrn=None, name=None, page=None):
        seen["name"] = name
        return _Outcome({"status": "ok"})

    def fake_history(case_number=None, **kw):
        seen["case_number"] = case_number
        return _Outcome({"status": "ok"})

    def fake_text(file_path=None):
        seen["file"] = file_path
        return _Outcome({"status": "ok"})

    monkeypatch.setattr(public_api, "search_practice", fake_practice)
    monkeypatch.setattr(public_api, "kad_search", fake_kad)
    monkeypatch.setattr(public_api, "case_history", fake_history)
    monkeypatch.setattr(public_api, "get_decision_text", fake_text)
    code, out, err = run_cli(argv)
    assert code == 0, err
    assert json.loads(out)["status"] == "ok"


@pytest.mark.parametrize("argv", [
    ["--json", "practice", "запрос"],
    ["--json", "case-history", "А40-1/2026"],
])
def test_live_command_fails_closed_by_fact(tmp_path, monkeypatch, argv):
    """Unreachable network/proxy surfaces as a typed error with the cause."""
    import sud_cli.case_search.public_api as public_api

    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)

    def failing_practice(query=None, **kw):
        return _Outcome({"status": "error", "reason": "network unreachable via proxy"})

    def failing_history(case_number=None, **kw):
        return _Outcome({"status": "error", "reason": "network unreachable via proxy"})

    monkeypatch.setattr(public_api, "search_practice", failing_practice)
    monkeypatch.setattr(public_api, "case_history", failing_history)
    code, out, err = run_cli(argv)
    assert code == 1
    assert out == ""
    payload = json.loads(err)
    assert payload["status"] == "error"
    assert "unreachable" in payload["data"]["reason"]


def test_npa_download_runs_without_env(tmp_path, monkeypatch):
    """npa download is a live core command: no env opt-in, fake transport only."""
    import sud_cli.npa_downloader as npa_downloader

    from tests.test_npa_download import EO, FakeTransport, _pdf_bytes

    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)
    monkeypatch.setattr(npa_downloader, "default_transport",
                        FakeTransport(body=_pdf_bytes("NPA text")))
    code, out, err = run_cli(["--json", "npa", "download", "--eo-number",
                              EO, "--out-dir", str(tmp_path / "npa")])
    assert code == 0, err
    envelope = json.loads(out)
    assert envelope["status"] == "ok"
    assert envelope["data"]["document"]["identifier"] == EO
