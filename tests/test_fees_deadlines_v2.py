# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Скелет cement-тестов аддендума v2 (fees/deadlines Wave 7).

Проверяет контракт загрузчика legal_rules_v2: fail-closed на каждой
деградации (файла нет / статус PENDING / схема сломана) и открытие
только на VERIFIED_BY_LEX_LEGAL с полным набором блоков.

Правовое содержание НЕ проверяется здесь — оно приходит от colleague
в LEGAL-SPEC-ADDENDUM-2.0.0.md и верифицируется им же; интеграционные
векторы по реальным нормам добавятся после поставки аддендума.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sud_cli.legal_engine import LegalRulesError
from sud_cli.legal_rules_v2 import REQUIRED_BLOCKS, load_addendum


def _write(tmp_path, payload) -> "Path":
    p = tmp_path / "legal_rules_addendum_v2.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _verified_payload() -> dict:
    data: dict = {
        "version": "2.0.0",
        "legal_status": "VERIFIED_BY_LEX_LEGAL_20261105",
        "spec_source": "LEGAL-SPEC-ADDENDUM-2.0.0.md",
    }
    for block, required in REQUIRED_BLOCKS.items():
        data[block] = {field: {"stub": True} for field in required}
    return data


def test_missing_addendum_fails_closed(tmp_path):
    with pytest.raises(LegalRulesError, match="addendum missing"):
        load_addendum(tmp_path / "nope.json")


def test_pending_status_fails_closed(tmp_path):
    p = _write(tmp_path, {**_verified_payload(), "legal_status": "PENDING_VERIFICATION"})
    with pytest.raises(LegalRulesError, match="addendum not verified"):
        load_addendum(p)


def test_missing_status_fails_closed(tmp_path):
    payload = _verified_payload()
    del payload["legal_status"]
    p = _write(tmp_path, payload)
    with pytest.raises(LegalRulesError, match="addendum not verified"):
        load_addendum(p)


@pytest.mark.parametrize("block", sorted(REQUIRED_BLOCKS))
def test_missing_block_fails_closed(tmp_path, block):
    payload = _verified_payload()
    del payload[block]
    p = _write(tmp_path, payload)
    with pytest.raises(LegalRulesError, match=f"block '{block}' missing"):
        load_addendum(p)


@pytest.mark.parametrize("block", sorted(REQUIRED_BLOCKS))
def test_missing_block_field_fails_closed(tmp_path, block):
    payload = _verified_payload()
    payload[block] = {}
    p = _write(tmp_path, payload)
    with pytest.raises(LegalRulesError, match=f"block '{block}' missing field"):
        load_addendum(p)


def test_verified_addendum_loads(tmp_path):
    p = _write(tmp_path, _verified_payload())
    data = load_addendum(p)
    assert data["version"] == "2.0.0"
    for block in REQUIRED_BLOCKS:
        assert block in data


def test_bom_tolerated(tmp_path):
    p = tmp_path / "legal_rules_addendum_v2.json"
    p.write_text(
        "\ufeff" + json.dumps(_verified_payload()), encoding="utf-8"
    )
    assert load_addendum(p)["version"] == "2.0.0"


def test_non_object_top_level_fails_closed(tmp_path):
    p = tmp_path / "legal_rules_addendum_v2.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(LegalRulesError, match="top level"):
        load_addendum(p)
