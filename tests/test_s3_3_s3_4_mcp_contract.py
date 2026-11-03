# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-3/S3-4: серверная валидация inputSchema и JSON-RPC -32602 на неизвестный тул."""
from __future__ import annotations

from sud_cli import mcp_server


def _call(name, args):
    return mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}})


def test_unknown_tool_is_jsonrpc_error():
    r = _call("sud_cli_nonexistent_tool", {})
    assert "error" in r
    assert r["error"]["code"] == -32602
    assert "result" not in r


def test_missing_required_argument():
    r = _call("sud_cli_limitation", {"period_type": "general"})
    assert r["error"]["code"] == -32602
    assert "start" in r["error"]["message"]


def test_unknown_argument_rejected():
    r = _call("sud_cli_limitation",
              {"start": "2020-01-01", "period_type": "general", "bogus_arg": 1})
    assert r["error"]["code"] == -32602
    assert "bogus_arg" in r["error"]["message"]


def test_enum_is_handler_owned():
    # enum/алиасы — документированный контракт хендлера (fees), гейт их
    # не трогает; семантика остаётся в движке (MANUAL_LEGAL_REVIEW и т.п.).
    r = _call("sud_cli_limitation",
              {"start": "2020-01-01", "period_type": "nonexistent_period",
               "check_date": "2021-01-01"})
    assert "result" in r  # дошло до движка, тот отвечает статусом/ошибкой


def test_bool_rejected_at_gate():
    r = _call("sud_cli_limitation",
              {"start": "2020-01-01", "period_type": "general",
               "claim_notice_days": True})
    assert r["error"]["code"] == -32602


def test_valid_call_still_works():
    r = _call("sud_cli_limitation",
              {"start": "2020-01-01", "period_type": "general",
               "check_date": "2021-01-01"})
    assert "result" in r
    assert r["result"]["isError"] is False


def test_draft_out_rejected_for_mcp():
    # S3-2: запись файлов — привилегия CLI; MCP получает контент.
    r = _call("sud_cli_draft", {"doc_type": "petition_deferral", "out": "/tmp/evil.md"})
    assert r["error"]["code"] == -32602
    assert "out" in r["error"]["message"]
