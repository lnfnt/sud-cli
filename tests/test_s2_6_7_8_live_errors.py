# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S2-6/7/8: лайв-парсер sudrf — полный транспортный сбой не маскируется
под «дело не найдено»."""
from __future__ import annotations

import pytest

import sud_cli.case_search.sudrf.live as live


class TestLookupCaseNumberTransport:
    def test_all_dates_fail_raises_not_empty_ok(self, monkeypatch):
        def _boom(host, **kw):
            raise live.SudrfLiveError(f"HTTP 503 from {host}")
        monkeypatch.setattr(live, "fetch_listing", _boom)
        with pytest.raises(live.SudrfLiveError, match="transport failure"):
            live.lookup_case_number("А40-123456/2026", host="example.test")

    def test_partial_failure_still_returns_matches(self, monkeypatch):
        from sud_cli.case_search.sudrf.live import LiveListingRow
        row = LiveListingRow(case_number="А40-123456/2026",
                             case_id="1", case_uid="u1",
                             card_url="http://x/1", hearing_time="10:00")
        calls = {"n": 0}

        def _flaky(host, *, date=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise live.SudrfLiveError("HTTP 503")
            return [row]
        monkeypatch.setattr(live, "fetch_listing", _flaky)
        found = live.lookup_case_number("А40-123456/2026",
                                        host="example.test", date_window=2)
        assert found == [row]

    def test_all_empty_success_means_not_found(self, monkeypatch):
        monkeypatch.setattr(live, "fetch_listing",
                            lambda host, **kw: [])
        assert live.lookup_case_number("А40-1/2026",
                                       host="example.test") == []
