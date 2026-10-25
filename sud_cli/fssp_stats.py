"""S-61 (В) официальная статистика БДИП ФССП — агрегатный справочник.

Акт legal 2026.10.25: общедоступный CSV статистики исполнительных производств
(fssp.gov.ru/api/is/statistics/csv) годен как вспомогательный источник по
ст. 7 8-ФЗ (достоверность оперативной информации). Для индивидуальных досье
статистика проверку НЕ заменяет — только контекст/справки.

Сетевой слой: тот же netguard-путь, что и остальные live-адаптеры
(fail-closed egress, DNS-pin, no-redirect, size cap, per-host breaker на
401/403/429/5xx-лимиты — ФССП ограничивает частые подключения, это штатно
уводится в cooldown, не брутфорсится).
"""
from __future__ import annotations

import csv
import io
from typing import Optional

STATS_URL = "https://fssp.gov.ru/api/is/statistics/csv"

_UA = ("Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/126.0")


class FsspStatsError(Exception):
    """Без PII: статистика не содержит персональные данные по построению."""


def _http_get(url: str, *, timeout: int = 30) -> bytes:
    from sud_cli.sud_netguard import (
        BreakerOpenError,
        breaker_trip,
        check_before_fetch,
    )

    try:
        check_before_fetch(url)
    except BreakerOpenError as exc:
        raise FsspStatsError(f"host in cooldown (anti-ban breaker): {exc}") from exc

    import urllib.error as _ue
    import urllib.request as _ur

    req = _ur.Request(url, headers={"User-Agent": _UA, "Accept": "text/csv,*/*"})

    class _NoRedirect(_ur.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        with _ur.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
            raw = resp.read(8_000_000)
    except _ue.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308, 401, 403, 429) or exc.code >= 500:
            breaker_trip(url, reason=f"fssp_stats_http_{exc.code}")
        raise FsspStatsError(f"fssp stats http {exc.code}") from exc
    except _ue.URLError as exc:
        raise FsspStatsError("fssp stats unreachable: " + str(exc.reason)[:80]) from exc
    return raw


def _parse(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    rows: list[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=";", quotechar='"')
    except Exception as exc:  # noqa: BLE001
        raise FsspStatsError("fssp stats: unparseable csv") from exc
    for row in reader:
        rid = (row.get("id") or "").strip()
        title = (row.get("title") or "").strip()
        count = (row.get("count") or "").strip()
        if not rid or not title:
            continue
        try:
            n = int(count)
        except ValueError:
            continue
        rows.append({"region_id": rid, "region": title, "active_executions": n})
    if not rows:
        raise FsspStatsError("fssp stats: unexpected payload (no rows)")
    return rows


def statistics(region: Optional[str] = None, *, timeout: int = 30) -> dict:
    """Общедоступная статистика БДИП: число активных ИП по подразделениям/регионам.

    region — опциональный фильтр по id региона (префикс) или названию (подстрока).
    """
    raw = _http_get(STATS_URL, timeout=timeout)
    rows = _parse(raw)
    total = sum(r["active_executions"] for r in rows if r["region_id"] == "00")
    if region:
        needle = region.strip().lower()
        rows = [r for r in rows
                if r["region_id"] == needle or needle in r["region"].lower()]
    return {
        "source": STATS_URL,
        "provider_label": "fssp.gov.ru (официальная статистика БДИП, ст. 7 8-ФЗ — оперативные данные)",
        "region_filter": region,
        "count": len(rows),
        "rows": rows,
        "note": ("Агрегатная статистика; индивидуальные проверки — ручной контур "
                 "fssp check/record (юридическая проверка 25.10)."),
        "center_total_hint": total,
    }
