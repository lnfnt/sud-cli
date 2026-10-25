"""v0.1.0 AUDIT-FIXES: контейainment purge, пустой default-trusted, маска proxy-credential."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sud_cli import db as dbm
from sud_cli import sud_netguard as ng


def test_trusted_default_is_empty():
    assert ng._TRUSTED_DEFAULT == set()


def test_trusted_only_via_env(monkeypatch):
    monkeypatch.delenv("SUD_NETGUARD_TRUSTED", raising=False)
    assert "198.51.100.1:11434" not in ng._trusted()
    monkeypatch.setenv("SUD_NETGUARD_TRUSTED", "ollama.local:11434")
    assert "ollama.local:11434" in ng._trusted()


def test_purge_contains_within_state_dir(tmp_path, monkeypatch):
    """Файл вне state_dir НЕ удаляется даже если записан в case_documents."""
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "important_outside.txt"
    outside.write_text("do not delete")
    inside = state / "doc.txt"
    inside.write_text("case doc")

    dbm.init_db(state)
    conn = dbm.get_connection(state)
    cid = dbm.create_case(conn, "А40-99999/25", "АС Москвы")["case_id"]
    dbm.add_case_document(conn, cid, str(inside), title="in")
    dbm.add_case_document(conn, cid, str(outside), title="out")
    res = dbm.purge_case(conn, state, cid, actor="test", remove_files=True)
    conn.close()
    assert outside.exists(), "файл вне state_dir выжил (containment)"
    assert not inside.exists(), "файл внутри state_dir удалён"
    assert res["files"]["outside_statedir"] == 1


def test_cw_config_masks_proxy_credentials():
    from sud_cli.case_search import config as cwc
    masked = cwc._mask_url("http://user:pass@proxy.internal:3128")
    assert "user:pass" not in masked
    assert "proxy.internal:3128" in masked
    assert cwc._mask_url("") == ""


def test_no_duplicate_watchentry():
    src = (Path(__file__).resolve().parent.parent / "sud_cli/case_search/config.py").read_text(encoding="utf-8")
    assert src.count("class WatchEntry:") == 1
    assert src.count('DEFAULT_SOURCE = "synthetic"') == 1
