# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Загрузчик аддендума v2 по госпошлинам и процессуальным срокам.

Wave 7 / fees-deadlines v2. Аддендум поставляется colleague как отдельный файл
(LEGAL-SPEC-ADDENDUM-2.0.0.md + машинный legal_rules_addendum_v2.json) и НЕ
патчит VERIFIED-файл legal_rules.json 1.3.0.

Fail-closed по Law Zero:
- файла нет            -> LegalRulesError("addendum missing")
- статус != VERIFIED   -> LegalRulesError("addendum not verified")
- схема блока сломана  -> LegalRulesError("addendum invalid")

До верификации legal v2-команды sud-cli обязаны отказывать с явным статусом,
а не «молчать или врать».
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .legal_engine import LegalRulesError

ADDENDUM_PATH = Path(__file__).with_name("legal_rules_addendum_v2.json")

#: Статус, которым colleague маркирует принятую и проверенную спецификацию.
VERIFIED_PREFIX = "VERIFIED_BY_LEX_LEGAL"

#: Блоки бэклога Wave 7 (письмо coordinator -> colleague 03.11.2026).
#: Ключ — имя блока в JSON, значение — обязательные поля блока.
REQUIRED_BLOCKS: Dict[str, tuple] = {
    "bankruptcy_deadlines": ("version", "norms_source", "deadlines"),
    "kas_special": ("version", "norms_source", "deadlines"),
    "consumer": ("version", "norms_source", "exemptions", "deadlines"),
    "corporate_as": ("version", "norms_source", "fees", "deadlines"),
    "regional_calendar": ("version", "norms_source", "regions"),
}


def load_addendum(path: Path = ADDENDUM_PATH) -> Dict[str, Any]:
    """Загрузить и проверить аддендум v2. Fail-closed на каждом шаге."""
    if not path.is_file():
        raise LegalRulesError(
            f"addendum missing: {path.name} (ожидается поставка от colleague)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegalRulesError(f"addendum unreadable: {exc}") from exc

    if not isinstance(data, dict):
        raise LegalRulesError("addendum invalid: top level must be an object")

    status = str(data.get("legal_status", ""))
    if not status.startswith(VERIFIED_PREFIX):
        raise LegalRulesError(
            f"addendum not verified: legal_status={status!r} "
            f"(требуется {VERIFIED_PREFIX}*)"
        )

    version = data.get("version")
    if not version:
        raise LegalRulesError("addendum invalid: version is required")

    for block, required in REQUIRED_BLOCKS.items():
        payload = data.get(block)
        if not isinstance(payload, dict):
            raise LegalRulesError(f"addendum invalid: block {block!r} missing")
        for field in required:
            if field not in payload:
                raise LegalRulesError(
                    f"addendum invalid: block {block!r} missing field {field!r}"
                )
    return data


def addendum_available(path: Path = ADDENDUM_PATH) -> bool:
    """True только если аддендум существует и прошёл все гейты."""
    try:
        load_addendum(path)
    except LegalRulesError:
        return False
    return True
