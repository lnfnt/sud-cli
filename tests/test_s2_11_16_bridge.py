# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S2-11..16: bridge и vendored court-watcher — regex-якорь, санация,
PATH-hijack, efrsb case_id, абсолютные дефолты store/alerts."""
from __future__ import annotations

import pytest

from sud_cli.court_watcher_bridge import _SEARCH_TEXT_RE, _validate_search_text, BridgeError


def test_search_text_rejects_trailing_newline():
    # S2-11: `$` пропускал «текст\n»; якорь \Z — только самый конец строки.
    assert not _SEARCH_TEXT_RE.match("текст\n")
    assert _SEARCH_TEXT_RE.match("текст")


def test_search_text_accepts_valid_phrase():
    assert _SEARCH_TEXT_RE.match("ООО Ромашка")
    with pytest.raises(BridgeError):
        _validate_search_text("a; rm -rf x", "query")


def test_sudrf_import_in_sanitation_set():
    import sud_cli.court_watcher_bridge as bridge
    src = open(bridge.__file__, encoding="utf-8").read()
    assert '"sudrf-import", "auto-import"' in src or "'sudrf-import', 'auto-import'" in src


def test_bridge_rejects_relative_binary_path():
    import sud_cli.court_watcher_bridge as bridge
    res = bridge.run_bridge_command(
        "import", state_dir=__import__("pathlib").Path("/tmp/x-bridge-test"),
        binary_path="./evil/court-watcher")
    assert res["status"] == "blocked"


def test_bridge_resolves_bare_binary_name():
    import sud_cli.court_watcher_bridge as bridge
    res = bridge.run_bridge_command(
        "definitely-not-a-command", state_dir=__import__("pathlib").Path("/tmp/x-bridge-test"))
    assert res["status"] == "blocked"  # дошло до валидации команды — резолв не упал


def test_efrsb_case_id_rejects_query_chars():
    from sud_cli.efrsb_public import EfrsbPublicError, case_publications
    for bad in ("1?x=1", "a&b", "x#frag", "../etc", "a/b"):
        with pytest.raises(EfrsbPublicError):
            case_publications(bad)
    # легитимный numeric id проходит валидацию (сеть замокана отсутствует — ошибка будет сетевой)
    import unittest.mock as mock
    with mock.patch("sud_cli.efrsb_public._api_get", return_value={"pageData": []}):
        r = case_publications("123")
    assert r["status"] == "not_found"


def test_vendored_cli_defaults_absolute():
    import sud_cli.case_search.cli as cw_cli
    import inspect
    src = inspect.getsource(cw_cli)
    assert 'default=".court-watcher/snapshots"' not in src
    assert 'default=".court-watcher/alerts"' not in src
