# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S1-5: pretrial неизвестный код / пустая rule — fail-visible, без выдуманного срока."""
from __future__ import annotations

from sud_cli.pretrial import calc_pretrial


def test_unknown_code_fails_visible():
    res = calc_pretrial("no_such_code", "2026-01-10")
    assert res.get("error") == "unknown_pretrial_code"
    assert "response_deadline" not in res
    assert any("manual_review" in w for w in res.get("warnings", []))


def test_absent_rule_fails_visible():
    rules = {"pretrial": {}, "disclaimer": "d"}
    res = calc_pretrial("apk", "2026-01-10", rules=rules)
    assert res.get("error") == "pretrial_rule_absent"


def test_known_code_still_computes():
    rules = {"pretrial": {"APK_30d": {"days": 30, "calendar": True,
                                      "exclude_nonworking_holidays": False}},
             "disclaimer": "d"}
    res = calc_pretrial("apk", "2026-01-10", rules=rules)
    assert res.get("response_deadline") == "2026-02-09"
    assert "error" not in res
