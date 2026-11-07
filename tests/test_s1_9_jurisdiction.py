# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S1-9: jurisdiction — ложные ветки по голым подстрокам и пустой ok на
неизвестном коде."""
from __future__ import annotations

from sud_cli.jurisdiction import check_jurisdiction


def test_bare_21_in_claim_does_not_trigger_vs_first():
    res = check_jurisdiction(
        "KAS", claim="Иск о взыскании 21 000 рублей по договору")
    venues = [str(c.get("venue_basis") or "") for c in res["candidates"]]
    assert not any("ст.21" in v for v in venues), res["candidates"]


def test_explicit_statute_still_triggers():
    res = check_jurisdiction("KAS", claim="пересмотр по ст. 21 Закона о ВС")
    venues = [str(c.get("venue_basis") or "") for c in res["candidates"]]
    assert any("21" in v for v in venues) or res["manual_review"], res


def test_unknown_code_fail_visible():
    res = check_jurisdiction("XXX")
    assert res["status"] == "unknown_code"
    assert res["candidates"] == [] and res["manual_review"] == []
    assert any("unknown jurisdiction code" in w for w in res["warnings"])
