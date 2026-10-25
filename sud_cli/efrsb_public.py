"""ЕФРСБ (bankrot.fedresurs.ru) — банкротные дела ЮЛ, ИП и граждан.

v0.1.0 (2026-10-20, мандат владельца: «из ru-legal берём лучшее в sud-cli»).
Паттерн заимствован у AlsKozlov/ru-legal efrsb-коннектора (Apache-2.0) в
clean-room стиле: их API-карта, наш транспорт (sud_netguard fail-closed),
своя нормализация. Их коннектор не импортируется.

Их задокументированные уроки, воспринятые здесь:
  - custom User-Agent блокируется (HTTP 403) -> нужен обычный браузерный UA;
  - Referer обязателен;
  - не-JSON ответ => источник в degraded mode (не «данных нет»).

API (public, без ключей):
  GET /backend/companies   ?searchString=&limit=
  GET /backend/persons     ?searchString=&birthDate=&limit=
  GET /backend/cases/<id>/publications?limit=
  GET /backend/arbitrManagers?searchString=&inn=&sroName=
Ответ: {"pageData": [...], ...}
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.request import Request as _Request

EFRSB_BASE = "https://bankrot.fedresurs.ru"
EFRSB_API_BASE = f"{EFRSB_BASE}/backend"

#: обычный браузерный UA: custom UA (см. ru-legal разведку) даёт 403
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class EfrsbPublicError(Exception):
    """Ошибка транспорта/ответа ЕФРСБ (fail-closed, без тихих деградаций)."""


def _efrsb_http_get(url: str, *, timeout: int = 30) -> str:
    """GET с netguard-защитой: egress fail-closed + per-host breaker."""
    from sud_cli.sud_netguard import (
        BreakerOpenError,
        breaker_trip,
        check_before_fetch,
    )

    try:
        check_before_fetch(url)
    except BreakerOpenError as exc:
        raise EfrsbPublicError(
            f"host in cooldown (anti-ban breaker): {exc}") from exc

    import urllib.error as _ue
    import urllib.request as _ur

    req = _Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
        "Referer": f"{EFRSB_BASE}/",
    })
    class _NoRedirect(_ur.HTTPRedirectHandler):
        # v2.3.3 (внешний review): efrsb обязан НЕ следовать редиректам —
        # 302 от источника обходил DNS-pin netguard (redirect к внутренним хостам).
        def redirect_request(self, *a, **k):
            return None

    try:
        with _ur.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
            raw = resp.read(5_000_000)  # size-cap: WAF-страницы/бомбы не съедают память
    except _ue.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308, 403, 429):
            breaker_trip(url, reason=f"efrsb_http_{exc.code}")
        # PII: searchString (ФИО/ИНН) был в тексте ошибки -> stdout/логи. Только host+path.
        from urllib.parse import urlsplit
        loc = urlsplit(url)
        raise EfrsbPublicError(f"efrsb http {exc.code}: {loc.netloc}{loc.path}") from exc
    except _ue.URLError as exc:
        raise EfrsbPublicError("efrsb unreachable: " + str(exc.reason)[:80]) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _api_get(path: str, params: dict[str, Any], timeout: int = 30) -> dict:
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{EFRSB_API_BASE}{path}{'?' + qs if qs else ''}"
    body = _efrsb_http_get(url, timeout=timeout)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        # их урок: не-JSON = degraded источник; для агента это error, не пустота
        raise EfrsbPublicError("efrsb returned non-JSON (degraded source)") from exc
    if not isinstance(data, dict):
        raise EfrsbPublicError("efrsb returned unexpected payload type")
    return data


def _page_data(data: dict) -> list[dict]:
    # v2.3.2 (sa-1 F-07): JSON-ответ источника без pageData (WAF/QRATOR-обёртка,
    # {"pageData": null}) — это деградация источника, а не «данных нет».
    if "pageData" not in data or data["pageData"] is None:
        raise EfrsbPublicError("efrsb payload without pageData (degraded source)")
    items = data["pageData"]
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _row_url(item: dict, kind: str) -> Optional[str]:
    rid = item.get("id")
    return f"{EFRSB_BASE}/{kind}/{rid}" if rid else None


def _normalize(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name") or item.get("fullName") or item.get("lastName"),
        "inn": item.get("inn"),
        "ogrn": item.get("ogrn"),
        "category": item.get("category"),
        "bankrupt_type": item.get("type") or item.get("bankruptType"),
        "case_number": item.get("caseNumber"),
        "date": item.get("date") or item.get("publishDate"),
        "url": _row_url(item, "bankrupt/id") if item.get("id") else None,
        "raw_keys": sorted(k for k in item if k not in ("id", "name", "inn", "ogrn")),
    }


def _validate_search_args(**kwargs: Any) -> dict:
    clean = {k: v for k, v in kwargs.items() if v}
    if not clean:
        raise EfrsbPublicError("нужен хотя бы один поисковый параметр")
    if len(clean) > 1:
        # v2.3.3 (внешний review): API принимает один searchString — «inn or ogrn or name»
        # молча выбрасывал переданные остальные параметры. Fail-closed: просим один.
        raise EfrsbPublicError("параметры inn/ogrn/name взаимоисключающие: передайте ровно один")
    return clean


def search_companies(inn: str | None = None, ogrn: str | None = None,
                     name: str | None = None, limit: int = 5) -> dict:
    """Банкротный поиск ЮЛ/ИП. INN(10)/ИП INN(12)/OGRN(13|15)/name>=3."""
    args = _validate_search_args(inn=inn, ogrn=ogrn, name=name)
    if inn and not (inn.isdigit() and len(inn) in (10, 12)):
        raise EfrsbPublicError("ИНН должен быть 10 (ЮЛ) или 12 (ИП) цифр")
    if ogrn and not (ogrn.isdigit() and len(ogrn) in (13, 15)):
        raise EfrsbPublicError("ОГРН должен быть 13 (ЮЛ) или 15 (ИП) цифр")
    if name and len(name) < 3:
        raise EfrsbPublicError("name: минимум 3 символа")
    search = inn or ogrn or name
    data = _api_get("/companies", {"searchString": search, "limit": limit})
    rows = _page_data(data)
    return {"status": "ok" if rows else "not_found",
            "query": search, "count": len(rows),
            "results": [_normalize(r) for r in rows[:limit]]}


def search_persons(full_name: str, birth_date: str | None = None,
                   limit: int = 5) -> dict:
    """Банкротный поиск физлиц по ФИО; birthDate (YYYY-MM-DD) сужает выборку."""
    if len(full_name) < 3:
        raise EfrsbPublicError("full_name: минимум 3 символа")
    params: dict[str, Any] = {"searchString": full_name, "limit": limit}
    if birth_date:
        params["birthDate"] = birth_date
    data = _api_get("/persons", params)
    rows = _page_data(data)
    return {"status": "ok" if rows else "not_found",
            "query": full_name, "count": len(rows),
            "results": [_normalize(r) for r in rows[:limit]]}


def case_publications(case_id: str, limit: int = 10) -> dict:
    """Публикации по делу (для слежения за конкретным банкротством)."""
    if not case_id or "/" in case_id or ".." in case_id:
        raise EfrsbPublicError("case_id: ожидается идентификатор дела ЕФРСБ")
    data = _api_get(f"/cases/{case_id}/publications", {"limit": limit})
    rows = _page_data(data)
    return {"status": "ok" if rows else "not_found", "case_id": case_id,
            "count": len(rows),
            "results": [{
                "id": r.get("id"),
                "title": r.get("title"),
                "date": r.get("date") or r.get("publishDate"),
                "url": _row_url(r, "message"),
            } for r in rows[:limit]]}


def search_managers(search: str | None = None, inn: str | None = None,
                    sro: str | None = None, limit: int = 5) -> dict:
    """Арбитражные управляющие (поиск по ФИО / ИНН / СРО)."""
    args = _validate_search_args(search=search, inn=inn, sro=sro)
    params: dict[str, Any] = {"limit": limit}
    if search:
        params["searchString"] = search
    if inn:
        params["inn"] = inn
    if sro:
        params["sroName"] = sro
    data = _api_get("/arbitrManagers", params)
    rows = _page_data(data)
    return {"status": "ok" if rows else "not_found",
            "query": args, "count": len(rows),
            "results": [{
                "id": r.get("id"),
                "name": r.get("name") or r.get("fullName"),
                "inn": r.get("inn"),
                "sro": r.get("sroName") or r.get("sro"),
                "url": _row_url(r, "arbitrManager/id") if r.get("id") else None,
            } for r in rows[:limit]]}
