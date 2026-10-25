"""v2.16.0: полу-автомат ФССП — офлайн-тесты на фейковом runtime."""
import json

import pytest

from sud_cli import fssp_semi as fs


class FakeRuntime:
    """Подмена camofox HTTP: детерминированные страницы form->captcha."""

    def __init__(self, page_text, after_submit):
        self.page_text = page_text
        self.after_submit = after_submit
        self.submitted = False
        self.screens = 0
        self.typed = None

    def api(self, method, path, *, body=None, query=None, timeout=60, binary=False):
        if path == "/tabs" and method == "POST":
            return {"tabId": "TAB1"}
        if path.endswith("/snapshot"):
            return {"text": self.after_submit if self.submitted else self.page_text}
        if path.endswith("/evaluate"):
            self.submitted = True
            return {"result": {"steps": ["radio", "inn", "submit"], "submitted": True}}
        if path.endswith("/screenshot"):
            self.screens += 1
            return b"\x89PNG fake"
        if path.endswith("/type"):
            self.typed = body.get("text")
            return {"ok": True}
        if path.endswith("/click"):
            self.submitted = True
            return {"ok": True}
        if path.endswith("/wait") or path.endswith("/navigate"):
            return {"ok": True}
        if path.endswith("/tabs") and method == "GET":
            return {"tabs": []}
        raise AssertionError("unexpected " + path)


def _fake(monkeypatch, rt):
    monkeypatch.setattr(fs, "_api", rt.api)


FORM = "<html>ФССП России — поиск по банку данных: физические лица / юридические лица</html>"
CAPTCHA = "<html>Подтвердите, что вы не робот. Введите капчу</html>"
RESULT = "<html>Найдено исполнительных производств: 3</html>"
LIMIT = "<html>Вы превысили лимит на количество подключений к сайту</html>"


def test_start_captcha_flow(tmp_path, monkeypatch):
    rt = FakeRuntime(FORM, CAPTCHA)
    _fake(monkeypatch, rt)
    sess = fs.start(tmp_path, kind="ur", inn="7707083893")
    assert sess["status"] == "waiting_human_captcha"
    assert rt.submitted and sess["captcha_image"].endswith(".png")
    # resume
    rt2 = FakeRuntime(CAPTCHA, RESULT)
    rt2.submitted = False
    monkeypatch.setattr(fs, "_api", rt2.api)
    # type/click на resumed tab: submitted flag у нового фейка игнорируется resume'ом (он кликает сам)
    out = fs.resume(tmp_path, sess["session_id"], "ABC123")
    assert rt2.typed == "ABC123"
    assert out["status"] == "done"


def test_start_limited_breaker(tmp_path, monkeypatch):
    trips = []
    monkeypatch.setattr(fs, "_breaker_trip", lambda u: trips.append(u))
    rt = FakeRuntime(LIMIT, LIMIT)
    _fake(monkeypatch, rt)
    sess = fs.start(tmp_path, kind="fiz", name="Иванов Иван")
    assert sess["status"] == "limited"
    assert trips == [fs.FSSP_URL]


def test_start_requires_params(tmp_path):
    with pytest.raises(fs.FsspAssistError):
        fs.start(tmp_path, kind="ur")


def test_runtime_must_be_loopback(monkeypatch):
    monkeypatch.setenv("CSE_FSSP_RUNTIME", "http://example.com:9377")
    with pytest.raises(fs.FsspAssistError):
        fs._runtime_url()


def test_resume_session_not_found(tmp_path):
    with pytest.raises(fs.FsspAssistError):
        fs.resume(tmp_path, "deadbeef", "X")


def test_resume_requires_code(tmp_path, monkeypatch):
    rt = FakeRuntime(FORM, CAPTCHA)
    _fake(monkeypatch, rt)
    sess = fs.start(tmp_path, kind="ur", inn="1")
    with pytest.raises(fs.FsspAssistError):
        fs.resume(tmp_path, sess["session_id"], "")
