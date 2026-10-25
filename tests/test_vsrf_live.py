"""VS RF live catalog client tests (no network, no real credentials)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from case_search.errors import MalformedPayloadError
from case_search.vsrf import live
from case_search.vsrf.live import (
    VsrfAuthError,
    VsrfClient,
    VsrfLiveError,
    get_credentials,
)


def _make_config(mapping: dict[str, str]):
    """Return a config.get replacement backed by ``mapping``."""
    def _get(key: str, default: str = "") -> str:
        return mapping.get(key, default)
    return _get


class TestGetCredentials:
    def test_missing_raises(self, monkeypatch):
        monkeypatch.setattr(live._cfg, "get", _make_config({}))
        with pytest.raises(VsrfAuthError) as ctx:
            get_credentials()
        assert "vsrf credentials not configured" in str(ctx.value)
        assert "CSE_VSRF_USERNAME" in str(ctx.value)

    def test_present_returns_tuple(self, monkeypatch):
        monkeypatch.setattr(
            live._cfg, "get",
            _make_config({"vsrf_username": "example_user",
                          "vsrf_password": "example_password"})
        )
        user, password = get_credentials()
        assert user == "example_user"
        assert password == "example_password"


class TestLogin:
    def test_fail_closed_non_200(self, monkeypatch):
        monkeypatch.setattr(
            live._cfg, "get",
            _make_config({"vsrf_username": "u", "vsrf_password": "p"})
        )
        client = VsrfClient()
        monkeypatch.setattr(client, "_http_post", lambda _url, _data, **kw: (403, "forbidden"))
        with pytest.raises(VsrfAuthError) as ctx:
            client.login()
        assert "HTTP 403" in str(ctx.value)


class TestSearchCatalog:
    def test_requires_login(self):
        client = VsrfClient()
        with pytest.raises(VsrfAuthError) as ctx:
            client.search_catalog("foo")
        assert "not logged in" in str(ctx.value)

    def test_malformed_html(self, monkeypatch):
        client = VsrfClient()
        client._logged_in = True
        monkeypatch.setattr(
            client, "_http_post",
            lambda _url, _data, **kw: (200, "<html>bogus</html>")
        )
        with pytest.raises(MalformedPayloadError) as ctx:
            client.search_catalog("foo")
        assert "unrecognized catalog HTML structure" in str(ctx.value)


class TestCliVsrfLookup:
    def test_no_credentials_exits_3(self, monkeypatch, capsys):
        monkeypatch.setattr(live._cfg, "get", _make_config({}))
        from case_search import cli
        code = cli.main(["vsrf-lookup", "test query"])
        captured = capsys.readouterr()
        assert code == 3
        assert "vsrf credentials not configured" in captured.err
        assert "CSE_VSRF_USERNAME" in captured.err

    def test_no_credentials_never_prints_them(self, monkeypatch, capsys):
        monkeypatch.setattr(
            live._cfg, "get",
            _make_config({"vsrf_username": "example_user",
                          "vsrf_password": "example_password"})
        )
        from case_search import cli
        # Simulate a post-login failure; stderr must not leak the password.

        def fake_login(self):
            raise VsrfAuthError("bad credentials")

        monkeypatch.setattr(VsrfClient, "login", fake_login)
        code = cli.main(["vsrf-lookup", "test query"])
        captured = capsys.readouterr()
        assert code == 3
        assert "example_password" not in captured.out
        assert "example_password" not in captured.err
        assert "example_user" not in captured.out
        assert "example_user" not in captured.err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
