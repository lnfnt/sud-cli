"""v2.14.0 S-03 egrul.nalog.ru offline contract tests (fixture, no network)."""
import json

import pytest

from sud_cli import egrul_nalog as eg

SBER_ROW = {
    "c": "ПАО СБЕРБАНК",
    "g": "ПРЕЗИДЕНТ, ПРЕЗИДАТЕЛЬ: НЕКТО",
    "cnt": "1",
    "i": "7707083893",
    "k": "ul",
    "n": 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "СБЕРБАНК РОССИИ"',
    "o": "1027700132195",
    "p": "773601001",
    "r": "16.08.2002",
    "t": "TOKENROW",
    "pg": "1",
    "tot": "1",
    "rn": "Г.Москва",
}


class FakeHttp:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = []

    def __call__(self, method, url, *, body=None, timeout=25):
        self.calls.append((method, url, body))
        nxt = self.seq.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(eg.time, "sleep", lambda s: None)


def test_search_happy_path(no_sleep, monkeypatch):
    fake = FakeHttp([
        json.dumps({"t": "TOKEN123", "captchaRequired": False}),
        "\n<html>Ошибка</html>",  # pending → poll again
        json.dumps({"rows": [SBER_ROW]}),
    ])
    monkeypatch.setattr(eg, "_http", fake)
    res = eg.search("7707083893")
    assert res["rows"][0]["ogrn"] == "1027700132195"
    assert res["rows"][0]["inn"] == "7707083893"
    assert res["rows"][0]["kind"] == "юридическое лицо"
    assert res["rows"][0]["registration_date"] == "16.08.2002"
    assert res["source"] == "https://egrul.nalog.ru"
    # POST body contains query; polling URL carries only the server token
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts and "query=7707083893" in posts[0][2]
    assert fake.calls[1][1].startswith("https://egrul.nalog.ru/search-result/TOKEN123")


def test_captcha_required_no_pii(no_sleep, monkeypatch):
    fake = FakeHttp([json.dumps({"t": "TOKEN123", "captchaRequired": True})])
    monkeypatch.setattr(eg, "_http", fake)
    with pytest.raises(eg.EgrulError) as ei:
        eg.search("Иванов Иван Иванович")
    assert "captcha" in str(ei.value)
    assert "Иванов" not in str(ei.value)


def test_not_ready_after_retries(no_sleep, monkeypatch):
    fake = FakeHttp([json.dumps({"t": "TOKEN123", "captchaRequired": False})] + ["<html>Ошибка</html>"] * 4)
    monkeypatch.setattr(eg, "_http", fake)
    with pytest.raises(eg.EgrulError) as ei:
        eg.search("123")
    assert "not ready" in str(ei.value)


def test_query_garbage_fail_closed(no_sleep, monkeypatch):
    fake = FakeHttp(["<!DOCTYPE html><html>WAF</html>"])
    monkeypatch.setattr(eg, "_http", fake)
    with pytest.raises(eg.EgrulError):
        eg.search("ООО Ромашка")


def test_empty_query_rejected():
    with pytest.raises(eg.EgrulError):
        eg.search("   ")


def test_http_error_message_has_no_query(no_sleep, monkeypatch):
    import urllib.error as ue
    from io import BytesIO

    def boom(method, url, *, body=None, timeout=25):
        if method == "POST":
            raise ue.HTTPError("https://egrul.nalog.ru/", 429, "Too Many Requests", {}, BytesIO())
        raise AssertionError("unreachable")

    monkeypatch.setattr(eg, "_http", boom)
    with pytest.raises(Exception) as ei:
        eg._post_query("7707083893")
    assert "7707083893" not in str(ei.value)
