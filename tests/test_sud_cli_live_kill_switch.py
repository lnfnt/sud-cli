# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

import json

import pytest

from tests.conftest import run_cli


@pytest.mark.parametrize("argv", [
    ["--json", "practice", "запрос"],
    ["--json", "kad", "--name", "Иванов"],
    ["--json", "case-history", "А40-1/2026"],
    ["--json", "extract-text", "/tmp/x.html"],
])
def test_live_command_disabled_without_env(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)
    code, out, err = run_cli(argv)
    assert code == 1
    assert out == ""
    payload = json.loads(err)
    assert payload["status"] == "error"
    assert payload["data"] == "live-источники отключены (S2-1..S2-5: egress обходится)"


def test_npa_download_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SUD_CLI_LIVE_OK", raising=False)
    code, out, err = run_cli(["--json", "npa", "download", "--eo-number",
                              "0001202509240023"])
    assert code == 1
    assert out == ""
    payload = json.loads(err)
    assert payload["status"] == "error"
    assert payload["data"] == "live-источники отключены (S2-1..S2-5: egress обходится)"
