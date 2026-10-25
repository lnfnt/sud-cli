"""egrul.nalog.ru — открытая проверка контрагента из ЕГРЮЛ/ЕГРИП (ФНС России).

Контракт живой разведки 2026-10-25 (RU-egress, проверено на реальных записях):

    POST https://egrul.nalog.ru/
         form: query=<ИНН|ОГРН|название>, nameEq=, page=, vyp3CaptchaToken=
         -> JSON {"t": "<токен>", "captchaRequired": false}
    GET  https://egrul.nalog.ru/search-result/<t>?r=<anti-cache>
         -> JSON {"rows": [ {...}, ... ]}  (пустой/не-JSON ответ = ещё готовится)

Поля строки: c=краткое наименование, n=полное наименование, o=ОГРН, i=ИНН,
k=тип (ul/ип), g=орган управления (ФИО), p=ОКПО, r=дата регистрации, rn=регион.

Сведения ЕГРЮЛ/ЕГРИП — открытые данные ФНС (ст. 6 129-ФЗ, акты государственной
регистрации общедоступны). Выписка vyp3 (PDF/XML) намеренно НЕ реализована:
требует капчу и не нужна для карточки проверки.

Сетевой слой: netguard (fail-closed egress, DNS-pin, per-host breaker),
редиректы запрещены, размер ответа ограничен, строки ошибок без PII.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request as _Request

EGRUL_BASE = "https://egrul.nalog.ru"

#: обычный браузерный UA: сервер отдаёт JSON только непустому UA (разведка 25.10)
_UA = ("Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/126.0")

_MAX_ATTEMPTS = 4
_BACKOFF_S = (1.2, 2.0, 3.0)


class EgrulError(Exception):
    """Надёжное сообщение об ошибке без PII (запрос токена может содержать ИНН/ФИО)."""


def _http(method: str, url: str, *, body: Optional[str] = None, timeout: int = 25) -> str:
    """Один защищённый HTTP-обмен: netguard + no-redirect + size cap."""
    from sud_cli.sud_netguard import (
        BreakerOpenError,
        breaker_trip,
        check_before_fetch,
    )

    try:
        check_before_fetch(url)
    except BreakerOpenError as exc:
        raise EgrulError(f"host in cooldown (anti-ban breaker): {exc}") from exc

    import urllib.error as _ue
    import urllib.request as _ur

    headers = {
        "User-Agent": _UA,
        "Referer": f"{EGRUL_BASE}/",
        "Accept": "application/json, text/html;q=0.8",
    }
    data = body.encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = _Request(url, data=data, headers=headers, method=method)

    class _NoRedirect(_ur.HTTPRedirectHandler):
        # редирект от источника обходил бы DNS-pin netguard (см. efrsb v2.3.3)
        def redirect_request(self, *a, **k):
            return None

    try:
        with _ur.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
    except _ue.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308, 403, 429):
            breaker_trip(url, reason=f"egrul_http_{exc.code}")
        from urllib.parse import urlsplit
        loc = urlsplit(url)
        raise EgrulError(f"egrul http {exc.code}: {loc.netloc}{loc.path}") from exc
    except _ue.URLError as exc:
        raise EgrulError("egrul unreachable: " + str(exc.reason)[:80]) from exc
    text = raw.decode("utf-8", errors="replace")
    return text


def _post_query(query: str, *, exact_name: bool = False) -> str:
    body = urlencode({
        "query": query,
        "nameEq": "on" if exact_name else "",
        "page": "",
        "vyp3CaptchaToken": "",
    })
    text = _http("POST", f"{EGRUL_BASE}/", body=body)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EgrulError("egrul: unexpected answer to query (WAF or captcha wall)") from exc
    token = data.get("t")
    if not token or not isinstance(token, str):
        raise EgrulError("egrul: query rejected (no search token)")
    if data.get("captchaRequired"):
        raise EgrulError("egrul: captcha required, retry later")
    return token


def _norm_row(row: dict[str, Any]) -> dict[str, Any]:
    kind = {"ul": "юридическое лицо", "ip": "индивидуальный предприниматель"}.get(
        str(row.get("k")), row.get("k"))
    return {
        "short_name": row.get("c"),
        "full_name": row.get("n"),
        "ogrn": row.get("o"),
        "inn": row.get("i"),
        "kind": kind,
        "governing_body": row.get("g"),
        "okpo": row.get("p"),
        "registration_date": row.get("r"),
        "region": row.get("rn"),
    }


def search(query: str, *, exact_name: bool = False, timeout: int = 25) -> dict[str, Any]:
    """Найти организацию/ИП в ЕГРЮЛ/ЕГРИП по ИНН, ОГРН или наименованию.

    Возвращает {"query":..., "rows": [карточки...]} — только открытые сведения ФНС.
    """
    if not query or not str(query).strip():
        raise EgrulError("egrul: empty query")
    token = _post_query(str(query).strip(), exact_name=exact_name)
    last_text = ""
    for attempt in range(_MAX_ATTEMPTS):
        url = f"{EGRUL_BASE}/search-result/{token}?r={random.randint(10**8, 10**9 - 1)}"
        last_text = _http("GET", url, timeout=timeout)
        try:
            data = json.loads(last_text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "rows" in data:
            return {
                "query": str(query).strip(),
                "rows": [_norm_row(r) for r in data.get("rows") or []],
                "source": EGRUL_BASE,
                "provider_label": "egrul.nalog.ru (открытые сведения ФНС)",
            }
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_S[attempt])
    raise EgrulError("egrul: search result not ready (retry later)")
