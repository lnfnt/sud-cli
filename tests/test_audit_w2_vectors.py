# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Аудит w2 (infra 02.11) — cement-пробы F-02/F-03 (sud-cli)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sud_cli import court_watcher_bridge as bridge


class _FakeBreakerTrip:
    """Ловим вызовы breaker_trip без сети и без глобального состояния."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, str]] = []
        from sud_cli import lex_netguard

        def _spy(url, reason=""):
            self.calls.append((url, reason))

        monkeypatch.setattr(lex_netguard, "breaker_trip", _spy)


def test_f03_live_command_rejects_relative_binary_path(tmp_path, monkeypatch):
    """F-03: live-ветка моста теперь валидирует binary_path (раньше — нет)."""
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "1")
    res = bridge.run_bridge_command(
        "lookup", state_dir=tmp_path / "st", case_id="А40-12345/2026",
        binary_path="rel/path")
    assert res["status"] == "blocked", res
    assert "absolute" in res["data"]


def test_f03_flag_command_rejects_relative_binary_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_BRIDGE_LIVE", "1")
    flag_cmd = next(iter(bridge._FLAG_COMMANDS))
    res = bridge.run_bridge_command(
        flag_cmd, state_dir=tmp_path / "st", options={},
        binary_path="rel/path")
    assert res["status"] == "blocked", res


def test_f03_offline_still_rejects_relative_binary_path(tmp_path):
    """Регрессия: офлайн-ветка по-прежнему требует абсолютный путь."""
    res = bridge.run_bridge_command(
        "providers", state_dir=tmp_path / "st", binary_path="rel/path")
    assert res["status"] == "blocked", res
    assert "absolute" in res["data"]


def test_f02_npa_403_trips_breaker(tmp_path, monkeypatch):
    """F-02: HTTP 403 в npa_downloader трипует per-host breaker."""
    import urllib.error

    from sud_cli import npa_downloader

    spy = _FakeBreakerTrip(monkeypatch)

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    from sud_cli.lex_netguard import BreakerOpenError
    monkeypatch.setattr(
        "sud_cli.lex_netguard.check_before_fetch", lambda url: None)

    with pytest.raises(npa_downloader.NpaDownloadError):
        npa_downloader.default_transport(
            "GET", "https://publication.pravo.gov.ru/doc", {}, 5.0)
    assert any(r == "http_403" for _, r in spy.calls), spy.calls


def test_f02_pravo_429_trips_breaker(tmp_path, monkeypatch):
    """F-02: HTTP 429 в pravo_publication трипует per-host breaker."""
    import urllib.error

    from sud_cli import pravo_publication

    spy = _FakeBreakerTrip(monkeypatch)

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

    monkeypatch.setattr(pravo_publication, "_opener", _Opener())
    monkeypatch.setattr(
        "sud_cli.lex_netguard.check_before_fetch", lambda url: None)

    with pytest.raises(urllib.error.HTTPError):
        pravo_publication.default_transport(
            "GET", "https://pravo.gov.ru/", {}, 5.0)
    assert any(r == "http_429" for _, r in spy.calls), spy.calls
