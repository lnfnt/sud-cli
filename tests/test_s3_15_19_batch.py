# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-15..19: drafter normalization, updater, conflict, case_diff, MCP state dir."""
from __future__ import annotations

import json

import pytest

from sud_cli.case_diff import DiffError, diff_case_snapshots
from sud_cli.conflict import check
from sud_cli.drafter import draft_document


# ---------- S3-15: drafter ----------

def test_whitespace_only_required_field_blocks():
    payload = draft_document("claim_arbitration", court="   ", plaintiff="ООО А",
                             defendant="ООО Б", claim_amount="1000",
                             basis="основание", petitum="требование")
    assert payload["status"] == "missing_required_fields"
    assert "court" in payload["missing"]


def test_field_length_limit_enforced():
    with pytest.raises(ValueError, match="лимит"):
        draft_document("claim_arbitration", court="А" * 11000, plaintiff="ООО А",
                       defendant="ООО Б", claim_amount="1000",
                       basis="основание", petitum="требование")


def test_crlf_normalized():
    payload = draft_document("objections", court="Суд\n\n", case_number="А40-1/24",
                             plaintiff="ООО А", defendant="ООО Б",
                             basis="основание", petitum="требование")
    assert payload["status"] == "ok"


# ---------- S3-17: updater ----------

def test_check_update_version_ordering():
    from sud_cli import updater

    def runner(cmd, **kw):
        class P:
            returncode = 0
            stdout = "Available versions: 99.0.0, 0.8.1\n"
        return P()

    res = updater.check_update(runner=runner)
    assert res["update_available"] is True  # 99.0.0 > текущей
    assert updater._is_newer("0.10.0", "0.9.0")
    assert not updater._is_newer("0.9.0", "0.10.0")
    assert not updater._is_newer("0.9.0", "0.9.0")
    assert not updater._is_newer("0.9.0", "2.9.0")  # downgrade ≠ update


# ---------- S3-18: conflict ----------

def test_conflict_int_str_case_id_excludes_own_case():
    portfolio = [{"case_id": 5, "owner": "A", "plaintiff": "ООО Ромашка"}]
    cand = {"case_id": "5", "parties": ["ООО Ромашка"]}
    assert check(portfolio, cand) == []


def test_conflict_plaintiff_equals_defendant_detected():
    portfolio = [{"case_id": 1, "owner": "A", "plaintiff": "ООО Ромашка",
                  "defendant": "ооо ромашка"}]
    hits = check(portfolio, {"case_id": "2", "parties": ["Новая сторона"]})
    assert any(h["type"] == "d" for h in hits)


# ---------- S3-19: case_diff ----------

def _snap(parties, published, file_path):
    file_path.write_text(json.dumps({
        "case_number": "А40-1/24",
        "parties": parties,
        "events_timeline": [{"date": "2024-01-01", "name": "Слушание",
                             "published": published}],
    }), encoding="utf-8")
    return file_path


def test_case_diff_string_parties_not_char_diff(tmp_path):
    a = _snap({"plaintiff": "ООО Рога", "respondent": [], "third_parties": []},
              "true", tmp_path / "a.json")
    b = _snap({"plaintiff": ["ООО Рога"], "respondent": [], "third_parties": []},
              "true", tmp_path / "b.json")
    res = diff_case_snapshots(a, b)
    party_changes = [c for c in res["changes"] if c["path"].startswith("parties.")]
    assert party_changes == []  # строка == список из одной строки


def test_case_diff_published_string_false_not_truthy(tmp_path):
    a = _snap({"plaintiff": ["X"]}, "false", tmp_path / "a.json")
    b = _snap({"plaintiff": ["X"]}, "true", tmp_path / "b.json")
    res = diff_case_snapshots(a, b)
    tl = [t for t in res["timeline_changes"] if t["before"] is False and t["after"] is True]
    assert len(tl) == 1


def test_mcp_state_dir_matches_cli_resolution(tmp_path, monkeypatch):
    from sud_cli.mcp_server import _mcp_state_dir
    from sud_cli.config import get_state_dir
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path / "rel" / ".."))
    assert _mcp_state_dir() == get_state_dir()
