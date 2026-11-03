# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-8/S3-9: argv-слой CLI — утечка путей в rejected-tokens и rewrite по позиции."""
from __future__ import annotations

from sud_cli.cli import _first_command_token, _rejected_tokens, main


def test_rejected_tokens_drops_eq_form_path():
    kept = _rejected_tokens(["--state-dir=/home/user/secret", "--json", "fees"])
    assert "/home/user/secret" not in kept
    assert "fees" in kept


def test_rejected_tokens_drops_eq_form_url():
    kept = _rejected_tokens(["--source=https://evil.example.com/x", "verify"])
    assert "evil.example.com" not in kept


def test_rejected_tokens_keeps_plain_values():
    kept = _rejected_tokens(["fees", "--amount", "1000"])
    assert "1000" in kept and "fees" in kept


def test_deadlines_value_of_other_option_not_rewritten():
    # «deadlines» как ЗНАЧЕНИЕ чужого аргумента не должно триггерить rewrite:
    # без фикса argv.index вставлял «calculate» и argv превращался в мусор
    # (usage-ошибка 2); с фиксом доезжаем до движка (JSON-конверт, rc=4).
    from tests.conftest import run_cli
    code, out, err = run_cli(["--json", "fees", "--kind", "deadlines",
                              "--claim-amount", "1000", "--jurisdiction", "arbitration"])
    assert code == 1, (code, out[:200])  # error-envelope CLI, не usage-ошибка 2
    assert "INVALID_ARGUMENTS" in out + err  # значение доехало до движка


def test_deadlines_command_still_rewritten():
    # legacy «deadlines <key>» по-прежнему маршрутизируется в calculate.
    from tests.conftest import run_cli
    import json
    code, out, _ = run_cli(["--json", "deadlines", "list"])
    assert code == 0, out[:200]
    assert json.loads(out)["status"] == "ok"
