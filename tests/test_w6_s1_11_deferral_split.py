# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""W6/§3 (S1-11): отсрочка пошлины — разграничение АС/СОЮ, Пленум ВАС № 6."""
from __future__ import annotations

from sud_cli.deferral import generate_deferral


def test_arbitration_template_has_correct_plenum():
    r = generate_deferral("ООО Ромашка", "АС Москвы", 100000,
                          "отсутствием денежных средств", court_type="arbitration")
    assert r["status"] == "ok"
    assert "102 АПК РФ" in r["text"]
    assert "от 20.03.1997 № 6" in r["text"]
    assert "№ 20" not in r["text"]
    assert any("№ 6" in b for b in r["legal_basis"])


def test_general_template_has_no_apk_or_vas():
    r = generate_deferral("Иванов И.И.", "Мировой судья", 50000,
                          "трудным положением", court_type="general")
    assert r["status"] == "ok"
    assert "ГПК РФ" in r["text"] and "КАС РФ" in r["text"]
    assert "АПК" not in r["text"]
    assert "ВАС" not in r["text"]
    assert "333.41" in r["text"]


def test_default_court_type_is_arbitration():
    r = generate_deferral("ООО Ромашка", "АС", 1000, "причина")
    assert r["status"] == "ok" and r["court_type"] == "arbitration"


def test_invalid_court_type_is_invalid_arguments():
    r = generate_deferral("ООО Ромашка", "АС", 1000, "причина",
                          court_type="military")
    assert r["status"] == "INVALID_ARGUMENTS"
    assert "arbitration" in r["detail"]
