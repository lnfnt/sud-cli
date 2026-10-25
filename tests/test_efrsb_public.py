"""ЕФРСБ-коннектор (sud_cli/efrsb_public.py) — приёмка v0.1.0.

Изоляция: сеть НЕ трогается никогда — мок ставится на точный call-site
(sud_cli.efrsb_public._efrsb_http_get), как того требует методика
(мок-таргет == фактический call-site, урок nedviga test_geo_routing).
"""
import json
from unittest import mock

import pytest

from sud_cli import efrsb_public as ep

PAGE = {"pageData": [
    {"id": "101", "name": "ООО Рога", "inn": "7701001001", "ogrn": "1157700000001",
     "caseNumber": "А40-123456/2026", "date": "2026-10-01"},
    {"id": "102", "fullName": "ИП Копыта", "category": "ИП"},
]}


def _mock_get(payload, as_json=True):
    body = json.dumps(payload) if as_json else payload
    return mock.patch.object(ep, "_efrsb_http_get", return_value=body)


def test_companies_happy_path():
    with _mock_get(PAGE):
        r = ep.search_companies(inn="7701001001")
    assert r["status"] == "ok" and r["count"] == 2
    assert r["results"][0]["case_number"] == "А40-123456/2026"
    assert r["results"][0]["url"].endswith("/bankrupt/id/101")
    assert r["results"][1]["name"] == "ИП Копыта"


def test_companies_not_found_is_valid_answer():
    with _mock_get({"pageData": []}):
        r = ep.search_companies(name="Звезда")
    assert r == {"status": "not_found", "query": "Звезда", "count": 0,
                 "results": []}


def test_invalid_args_rejected_locally():
    with pytest.raises(ep.EfrsbPublicError, match="ИНН"):
        ep.search_companies(inn="12345")  # 5 цифр — не ЮЛ и не ИП
    with pytest.raises(ep.EfrsbPublicError, match="ОГРН"):
        ep.search_companies(ogrn="12345")
    with pytest.raises(ep.EfrsbPublicError):
        ep.search_companies()  # ни одного параметра
    with pytest.raises(ep.EfrsbPublicError):
        ep.case_publications("../etc")  # path-traversal не проходит


def test_non_json_is_error_not_empty():
    with _mock_get("<html>captcha</html>", as_json=False):
        with pytest.raises(ep.EfrsbPublicError, match="degraded"):
            ep.search_companies(inn="7701001001")


def test_publications_normalize():
    with _mock_get({"pageData": [
        {"id": "55", "title": "О введении наблюдения", "date": "2026-10-05"}]}):
        r = ep.case_publications("123", limit=10)
    assert r["status"] == "ok"
    assert r["results"][0]["url"].endswith("/message/55")


def test_managers_search():
    with _mock_get({"pageData": [{"id": "9", "name": "Иванов И.И.",
                                  "sroName": "Ассоциация А"}]}):
        r = ep.search_managers(sro="Ассоциация А")
    assert r["results"][0]["sro"] == "Ассоциация А"
    assert r["results"][0]["url"].endswith("/arbitrManager/id/9")


def test_transport_sends_browser_ua_and_referer():
    """Их урок: custom UA = 403. Наш транспорт обязан слать штатный UA + Referer."""
    captured = {}

    def fake_open(req, timeout):
        captured["headers"] = dict(req.header_items())
        captured["url"] = req.full_url

        class R:
            def read(self, _maxbytes=None):
                return json.dumps(PAGE).encode()

            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    with mock.patch("urllib.request.build_opener") as bo:
        bo.return_value.open.side_effect = fake_open
        ep.search_companies(inn="7701001001")
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs["user-agent"].startswith("Mozilla/5.0")
    assert hdrs["referer"] == ep.EFRSB_BASE + "/"
    assert captured["url"].startswith("https://bankrot.fedresurs.ru/backend/companies?")


def test_403_trips_breaker_and_fails_closed():
    import urllib.error as ue
    with mock.patch("urllib.request.build_opener") as bo, \
         mock.patch("sud_cli.sud_netguard.breaker_trip") as trip:
        bo.return_value.open.side_effect = ue.HTTPError(
            ep.EFRSB_API_BASE, 403, "Forbidden", None, None)
        with pytest.raises(ep.EfrsbPublicError, match="403"):
            ep.search_companies(inn="7701001001")
    assert trip.called
