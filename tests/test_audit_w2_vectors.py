# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Аудит w2 (infra 02.11) — cement-пробы F-02 (sud-cli).

F-03 покрывала transitional bridge к внешнему case-CLI; мост изъят волной W8
(репозиторий 2026-11-05), офлайн-валидация binary_path ушла вместе с ним.
F-02 (per-host breaker на HTTP 403/429 в npa/pravo) сохранена и закреплена здесь.
"""
from __future__ import annotations

import pytest


class _FakeBreakerTrip:
    """Ловим вызовы breaker_trip без сети и без глобального состояния."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, str]] = []
        from sud_cli import net_guard

        def _spy(url, reason=""):
            self.calls.append((url, reason))

        monkeypatch.setattr(net_guard, "breaker_trip", _spy)


def test_f02_npa_403_trips_breaker(tmp_path, monkeypatch):
    """F-02: HTTP 403 в npa_downloader трипует per-host breaker."""
    import urllib.error

    from sud_cli import npa_downloader

    spy = _FakeBreakerTrip(monkeypatch)

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    monkeypatch.setattr(
        "sud_cli.net_guard.check_before_fetch", lambda url: None)

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
        "sud_cli.net_guard.check_before_fetch", lambda url: None)

    with pytest.raises(urllib.error.HTTPError):
        pravo_publication.default_transport(
            "GET", "https://pravo.gov.ru/", {}, 5.0)
    assert any(r == "http_429" for _, r in spy.calls), spy.calls
