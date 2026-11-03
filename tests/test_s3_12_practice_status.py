# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-12: practice_status — частичная отмена не гасится негацией; stem
вида «А40» не привязывает чужие дела."""
from __future__ import annotations

import json
from pathlib import Path

from sud_cli.practice_status import has_overturn_marker, scan_store_dir


def test_partial_overturn_not_negated():
    assert has_overturn_marker(
        "Определением апелляции решение отменено в части, в остальной части без изменения")


def test_full_negation_still_false():
    assert not has_overturn_marker(
        "Определением апелляции решение оставлено без изменения")


def test_short_stem_does_not_claim_foreign_case(tmp_path):
    foreign = tmp_path / "А40.json"
    foreign.write_text(json.dumps({
        "case_number": "А40-999/2026",
        "title": "решение отменено",
    }, ensure_ascii=False), encoding="utf-8")
    hits = scan_store_dir(tmp_path, "А40-123456/2026")
    assert hits == [], f"чужое дело привязалось: {hits}"


def test_full_number_stem_claims_case(tmp_path):
    own = tmp_path / "А40-123456-2026.json"
    own.write_text(json.dumps({
        "case_number": "А40-123456/2026",
        "title": "решение отменено с направлением на новое рассмотрение",
    }, ensure_ascii=False), encoding="utf-8")
    hits = scan_store_dir(tmp_path, "А40-123456/2026")
    assert hits, "свой кейс не найден"
