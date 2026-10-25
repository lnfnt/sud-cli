"""SudRF live adapter: listings, cards, case-number lookup.

Verified live 2026-09-15 through Finnish residential proxy:
- ``https://{host}/modules.php?name=sud_delo`` — hearings listing
  (per date, ``H_date=DD.MM.YYYY``), each row carries
  ``case_id``, ``case_uid`` and the case number;
- ``...&name_op=case&case_id=...&case_uid=...`` — full case card
  (УИД, ДВИЖЕНИЕ ДЕЛА, УЧАСТНИКИ) — parsed by the existing
  ``sudrf.parser`` (offline importer).

Host naming is heterogeneous: oblast courts use single-dash
``oblsud.{region}.sudrf.ru``; district courts use double-dash
``{court}--{region}.sudrf.ru``. The live registry
(``registry.json``) tracks verified hosts; ``resolve_host`` also
tries both schemes on the fly.

No GET search-by-number exists in GAS. ``lookup_case_number``
resolves a number via the listing scan (date window around the
case year, heuristic) and falls back to an error when the listing
does not contain the number.

Resource hygiene: every fetch closes its HTTP response in a
``try/finally``; the module holds no browser state and works over
plain HTTP (curl-compatible), optionally through a proxy.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from case_search._logging import get_logger
from case_search.errors import MalformedPayloadError
from case_search.normalize import normalize
from case_search.schema import CaseSnapshot

from . import cache as _cache

SUDRF_LIVE_VERSION = "sudrf-live-0.1.0"
_log = get_logger("sudrf.live")

_REGISTRY_PATH = Path(__file__).parent / "registry.json"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
_ROW_RE = re.compile(
    r"href='(/modules\.php\?name=sud_delo[^']*case_id=(?P<cid>\d+)"
    r"[^']*)'[^>]*><u>(?P<num>[^<]+)</u>")
_UID_RE = re.compile(r"case_uid=([0-9a-f-]{36})")


class SudrfLiveError(Exception):
    """Live sudrf failure (host dead, listing empty, case not found)."""


@dataclass(frozen=True)
class LiveListingRow:
    case_number: str
    case_id: str
    case_uid: str
    card_url: str
    hearing_time: str

    def to_dict(self) -> dict[str, str]:
        return {"case_number": self.case_number,
                "case_id": self.case_id, "case_uid": self.case_uid,
                "card_url": self.card_url,
                "hearing_time": self.hearing_time}


def load_registry() -> dict[str, Any]:
    """Load the verified-host registry (bundled JSON)."""
    return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))


def resolve_host(court: str, region: str) -> list[str]:
    """Return candidate hosts for a court, registry-first.

    ``court`` is e.g. ``oblsud`` or ``himki``; ``region`` is the
    GAS region code (``mo``, ``msk``, ...). Both known schemes are
    returned in probe order: registry match, single-dash, double-dash.
    """
    candidates: list[str] = []
    reg = load_registry()
    verified = reg.get("verified_hosts", {})
    for host in verified:
        if court in host and (f".{region}." in host
                              or f"--{region}." in host):
            candidates.append(host)
    for group in ("oblsud", "district"):
        for host in reg.get(group, {}):
            if court in host and (f".{region}." in host
                                  or f"--{region}." in host):
                candidates.append(host)
    if f"{court}.{region}.sudrf.ru" not in candidates:
        candidates.append(f"{court}.{region}.sudrf.ru")
    if f"{court}--{region}.sudrf.ru" not in candidates:
        candidates.append(f"{court}--{region}.sudrf.ru")
    return candidates


def _http_get(url: str, *, proxy: str | None = None,
              timeout: int = 30) -> tuple[int, str]:
    """Plain HTTP GET returning (status, decoded body). cp1251-aware.

    v0.1.1: защищено sud_netguard — egress-guard (fail-closed на приватные
    сети) + per-host breaker (cooldown после 403/429, анти-бан подсети).
    """
    from sud_cli.sud_netguard import (
        BreakerOpenError,
        breaker_trip,
        check_before_fetch,
    )

    try:
        check_before_fetch(url)
    except BreakerOpenError as exc:
        raise SudrfLiveError(
            f"host in cooldown (anti-ban breaker): {exc}"
        ) from exc
    handlers = []
    if proxy:
        handlers.append(urlopen.__globals__["ProxyHandler"](
            {"http": proxy, "https": proxy}))
    import urllib.request as _ur
    opener = _ur.build_opener(*handlers)
    req = Request(url, headers={"User-Agent": _UA})
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
    except _ur.HTTPError as exc:
        if exc.code in (403, 429):
            breaker_trip(url, reason=f"http_{exc.code}")
        raise
    try:
        return code, raw.decode("utf-8")
    except UnicodeDecodeError:
        return code, raw.decode("cp1251", errors="replace")


def fetch_listing(host: str, *, date: str | None = None,
                  proxy: str | None = None,
                  timeout: int = 30,
                  use_cache: bool = True) -> list[LiveListingRow]:
    """Fetch the hearings listing for ``host`` (optionally per date).

    Returns parsed rows; raises SudrfLiveError on HTTP failure or
    when the page carries no module marker.  Successful fetches are
    written through to the SQLite cache when ``use_cache`` is True.
    """
    url = f"https://{host}/modules.php?name=sud_delo"
    if date:
        url += f"&srv_num=1&H_date={quote(date)}"
    if use_cache:
        try:
            cached = _cache.get_cached_listing(host, date or "")
            if cached is not None:
                return [LiveListingRow(**row) for row in cached]
        except Exception as exc:
            _log.warning("cache read failed for listing %s %s: %s",
                         host, date, exc)
    code, body = _http_get(url, proxy=proxy, timeout=timeout)
    if code != 200:
        raise SudrfLiveError(f"HTTP {code} from {host}")
    if "sud_delo" not in body:
        raise SudrfLiveError(f"{host}: not a sud_delo module page")
    rows: list[LiveListingRow] = []
    for m in _ROW_RE.finditer(body):
        path = m.group("cid")
        card_path = m.group(1)
        num = m.group("num").strip()
        uid_m = _UID_RE.search(card_path)
        # hearing time: <td>HH:MM</td> right after the number cell
        tail = body[m.end():m.end() + 200]
        tm = re.search(r"(\d{2}:\d{2})", tail)
        rows.append(LiveListingRow(
            case_number=num, case_id=m.group("cid"),
            case_uid=uid_m.group(1) if uid_m else "",
            card_url=f"https://{host}{card_path}",
            hearing_time=tm.group(1) if tm else ""))
    if use_cache:
        try:
            _cache.put_listing(host, date or "", url,
                               [r.to_dict() for r in rows])
        except Exception as exc:
            _log.warning("cache write failed for listing %s %s: %s",
                         host, date, exc)
    return rows


def fetch_case_card(host: str, case_id: str, case_uid: str, *,
                    delo_id: str = "5", proxy: str | None = None,
                    timeout: int = 40,
                    use_cache: bool = True) -> str:
    """Fetch the full case-card HTML (УИД + ДВИЖЕНИЕ + УЧАСТНИКИ).

    Returns raw HTML; parsing goes through the existing
    ``sudrf.parser``. Raises SudrfLiveError on HTTP failure.
    Successful fetches are written through to the SQLite cache when
    ``use_cache`` is True.
    """
    if use_cache:
        try:
            cached = _cache.get_cached_card(case_uid)
            if cached is not None:
                return cached
        except Exception as exc:
            _log.warning("cache read failed for card %s: %s",
                         case_uid, exc)
    url = (f"https://{host}/modules.php?name=sud_delo&srv_num=1"
           f"&name_op=case&case_id={case_id}&case_uid={case_uid}"
           f"&delo_id={delo_id}&new=5")
    code, body = _http_get(url, proxy=proxy, timeout=timeout)
    if code != 200 or len(body) < 10000:
        raise SudrfLiveError(
            f"HTTP {code}, {len(body)}B — card fetch failed for "
            f"case_id={case_id}")
    if "ДВИЖЕНИЕ" not in body and "Движение" not in body:
        raise SudrfLiveError(f"case_id={case_id}: no motion table "
                             f"(card structure changed?)")
    if use_cache:
        try:
            _cache.put_card(case_uid, host, body)
        except Exception as exc:
            _log.warning("cache write failed for card %s: %s",
                         case_uid, exc)
    return body


def lookup_case_number(case_number: str, *, host: str,
                       date_window: int = 5,
                       explicit_dates: list[str] | None = None,
                       proxy: str | None = None,
                       timeout: int = 30,
                       use_cache: bool = True,
                       max_workers: int = 4) -> list[LiveListingRow]:
    """Resolve a case number to listing rows via a date-window scan.

    GAS has no GET search-by-number; the practical path is scanning
    the hearings listing for ``date_window`` days back from today
    (most hearings get listed same/next day).

    ``explicit_dates`` (list of ``DD.MM.YYYY``) adds known hearing
    dates to the scan — required for archived/completed cases whose
    hearings fall outside the recent window.

    The window is scanned in parallel (up to ``max_workers`` threads,
    capped at 4) with a small stagger between starts to be polite to
    GAS.  Each thread reuses the SQLite cache.
    """
    import datetime as _dt
    target = re.sub(r"[^0-9А-Яа-я/-]", "",
                    case_number.replace(" ", "")).strip()
    today = _dt.date.today()
    dates: list[str] = [day.strftime("%d.%m.%Y")
                        for day in (today - _dt.timedelta(days=o)
                                    for o in range(date_window))]
    if explicit_dates:
        dates = list(explicit_dates) + dates
    matches: list[LiveListingRow] = []

    def _fetch_one(date_str: str) -> list[LiveListingRow]:
        try:
            return fetch_listing(host, date=date_str, proxy=proxy,
                                 timeout=timeout, use_cache=use_cache)
        except SudrfLiveError:
            return []

    workers = min(max(1, max_workers), 4)
    results_by_date: dict[str, list[LiveListingRow]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_date: dict[Any, str] = {}
        for i, date_str in enumerate(dates):
            future_to_date[executor.submit(_fetch_one, date_str)] = date_str
            if i < len(dates) - 1:
                time.sleep(0.05)
        for future in future_to_date:
            date_str = future_to_date[future]
            try:
                results_by_date[date_str] = future.result()
            except Exception:
                results_by_date[date_str] = []

    for date_str in dates:
        rows = results_by_date.get(date_str, [])
        for row in rows:
            if row.case_number.replace(" ", "") == target or \
                    target in row.case_number:
                matches.append(row)
        if matches:
            break
    return matches


def list_registry_hosts() -> list[str]:
    """All hosts with confirmed live sud_delo listings."""
    reg = load_registry()
    hosts = reg.get("hosts", {})
    live = sorted(info["host"] for h, info in hosts.items()
                  if info.get("host") and
                  (info.get("verified_live")
                   or info.get("cases_in_listing", 0) > 0))
    if live:
        return live
    # legacy registry layouts
    verified = reg.get("verified_hosts", {})
    if verified:
        return sorted(verified)
    return sorted(list(reg.get("oblsud", {})) + list(reg.get("district", {})) +
                  list(reg.get("supreme", {})))


def parse_live_motion(card_html: str) -> list[dict[str, str]]:
    """Parse ДВИЖЕНИЕ ДЕЛА from a live case-card (tab layout).

    Row format: event | date | time | place | result | grounds |
    note | placement-date. Returns canonical event dicts.
    """
    out: list[dict[str, str]] = []
    m = re.search(r"Наименование события</b></td>(.*?)</table>", card_html, re.S)
    if not m:
        return out
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        tds = [re.sub(r"<[^>]+>", "", td).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) < 2 or not tds[0]:
            continue
        out.append({
            "event": tds[0],
            "date": tds[1] if len(tds) > 1 else "",
            "time": tds[2] if len(tds) > 2 else "",
            "place": tds[3] if len(tds) > 3 else "",
            "result": tds[4] if len(tds) > 4 else "",
            "grounds": tds[5] if len(tds) > 5 else "",
            "note": tds[6] if len(tds) > 6 else "",
            "placement": tds[7] if len(tds) > 7 else "",
        })
    return out


def parse_live_participants(card_html: str) -> list[dict[str, str]]:
    """Parse УЧАСТНИКИ from a live case-card.

    Row format: role | name | INN | KPP | OGRN | OGRNIP.
    GAS masks names as 'Информация скрыта' in some cards — such
    rows are returned with an empty name (masked=True marker).
    """
    out: list[dict[str, str]] = []
    m = re.search(r"Вид лица, участвующего в деле</b></td>(.*?)</table>",
                  card_html, re.S)
    if not m:
        return out
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        tds = [re.sub(r"<[^>]+>", "", td).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) < 2 or not tds[0]:
            continue
        masked = tds[1].startswith("Информация скрыта")
        out.append({
            "role": tds[0],
            "name": "" if masked else tds[1],
            "inn": tds[2] if len(tds) > 2 else "",
            "ogrn": tds[4] if len(tds) > 4 else "",
            "masked": "true" if masked else "false",
        })
    return out


def parse_live_card_header(card_html: str) -> dict[str, str]:
    """Parse the ДЕЛО header (УИД, категория, судья, результат)."""
    out: dict[str, str] = {}
    labels = ("Уникальный идентификатор дела", "Дата поступления",
              "Категория дела", "Судья", "Дата рассмотрения",
              "Результат рассмотрения")
    for label in labels:
        m = re.search(
            rf"<b>{label}</b>\s*</td>\s*<td[^>]*>(.*?)</td>",
            card_html, re.S)
        if m:
            val = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            out[label] = val
    return out


_SUDRF_ROLE_MAP = {
    "истец": "plaintiff",
    "ответчик": "respondent",
    "третье лицо": "third_parties",
    "административный истец": "plaintiff",
    "административный ответчик": "respondent",
    "заинтересованное лицо": "third_parties",
    "представитель": "third_parties",
}


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sudrf_raw_hash(result: dict[str, Any]) -> str:
    blob = json.dumps(result, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _ddmmyyyy_to_iso(date: str) -> str:
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", date.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return date.strip()


def sudrf_result_to_snapshot(result: dict[str, Any],
                             *, warnings: list[str] | None = None,
                             source: str = "sudrf-live"
                             ) -> CaseSnapshot:
    """Build a canonical CaseSnapshot from a SudRF live result dict.

    Maps the fields that have a clean, deterministic equivalent:
    ``case_number``, ``court`` (from ``court_name``/``host``),
    ``judge`` and ``status`` (from the card header), participants
    (aggregated by role), and ``events_timeline`` (from ``motion``).
    Heterogeneous details (``case_id``, ``case_uid``, ``uid``,
    hearing times, masked INN/OGRN) stay in ``extensions``.

    Fail-closed: missing ``case_number`` raises ``MalformedPayloadError``.
    """
    if not isinstance(result, dict):
        raise MalformedPayloadError(source, "result must be a dict")

    case_number = result.get("case_number")
    if not case_number:
        raise MalformedPayloadError(source, "missing field 'case_number'")

    header = result.get("header") or {}
    if not isinstance(header, dict):
        header = {}

    court = (result.get("court_name") or result.get("host") or "unknown")
    judge = header.get("Судья", "") or "unknown"
    status = header.get("Результат рассмотрения", "") or "active"

    merged_warnings: list[str] = list(warnings or [])
    participants: dict[str, list[str]] = {
        "plaintiff": [], "respondent": [], "third_parties": []}
    for party in result.get("participants", []):
        if not isinstance(party, dict):
            continue
        role = str(party.get("role", "")).strip()
        name = str(party.get("name", "")).strip()
        if not role or not name:
            continue
        bucket = _SUDRF_ROLE_MAP.get(role.lower())
        if bucket is None:
            bucket = "third_parties"
            merged_warnings.append(
                f"unmapped participant role '{role}' placed in third_parties")
        participants[bucket].append(name)

    events_timeline: list[dict[str, Any]] = []
    for idx, row in enumerate(result.get("motion", [])):
        if not isinstance(row, dict):
            merged_warnings.append(f"motion row {idx} skipped: not a dict")
            continue
        date = str(row.get("date", "")).strip()
        name = str(row.get("event", "")).strip()
        if not date or not name:
            merged_warnings.append(
                f"motion row {idx} skipped: missing date or event")
            continue
        iso_date = _ddmmyyyy_to_iso(date)
        if iso_date == date:
            merged_warnings.append(
                f"motion row {idx} date not in DD.MM.YYYY format: {date}")
        events_timeline.append({
            "date": iso_date,
            "name": name,
            "published": True,
            "pdf_url": "",
        })

    last_event = None
    if events_timeline:
        first = events_timeline[0]
        doc_url = str(result.get("card_url", "")).strip()
        if not doc_url:
            doc_url = "unknown"
        last_event = {
            "date": first["date"],
            "event_type": first["name"],
            "document_url": doc_url,
        }

    payload = {
        "case_number": str(case_number).strip(),
        "court": court,
        "judge": judge,
        "status": status,
        "participants": participants,
        "next_hearing": None,
        "last_event": last_event,
        "events_timeline": events_timeline,
        "updated_at": _utc_today_iso(),
    }

    return normalize(
        payload,
        source=source,
        source_url=str(result.get("card_url", "")),
        retrieved_at=_utc_now_iso(),
        raw_artifact_hash=_sudrf_raw_hash(result),
        warnings=tuple(merged_warnings),
        parser_version=SUDRF_LIVE_VERSION,
    )


def get_live_case(case_number: str, *, host: str, date_window: int = 5,
                  explicit_dates: list[str] | None = None,
                  proxy: str | None = None,
                  timeout: int = 30,
                  use_cache: bool = True,
                  max_workers: int = 4) -> dict[str, Any]:
    """Full live pipeline: number → listing → card → parsed data.

    Returns {case_number, case_id, case_uid, card_url, uid, header,
    motion, participants}. Raises SudrfLiveError when the case is
    not in the scanned listings.
    """
    rows = lookup_case_number(case_number, host=host,
                              date_window=date_window,
                              explicit_dates=explicit_dates,
                              proxy=proxy, timeout=timeout,
                              use_cache=use_cache,
                              max_workers=max_workers)
    if not rows:
        raise SudrfLiveError(
            f"case {case_number!r} not found in listings of {host} "
            f"(scanned {date_window} days; GAS has no GET search-by-number)")
    row = rows[0]
    card = fetch_case_card(host, row.case_id, row.case_uid, proxy=proxy,
                           timeout=timeout, use_cache=use_cache)
    header = parse_live_card_header(card)
    return {
        "case_number": row.case_number,
        "case_id": row.case_id,
        "case_uid": row.case_uid,
        "card_url": row.card_url,
        "host": host,
        "uid": header.get("Уникальный идентификатор дела", ""),
        "header": header,
        "motion": parse_live_motion(card),
        "participants": parse_live_participants(card),
        "parser_version": SUDRF_LIVE_VERSION,
    }


def parse_live_motion(card_html: str) -> list[dict[str, str]]:
    """Parse ДВИЖЕНИЕ ДЕЛА from a live case-card (tab layout).

    Row format: event | date | time | place | result | grounds |
    note | placement-date. Returns canonical event dicts.
    """
    out: list[dict[str, str]] = []
    m = re.search(r"Наименование события</b></td>(.*?)</table>", card_html, re.S)
    if not m:
        return out
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        tds = [re.sub(r"<[^>]+>", "", td).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) < 2 or not tds[0]:
            continue
        out.append({
            "event": tds[0],
            "date": tds[1] if len(tds) > 1 else "",
            "time": tds[2] if len(tds) > 2 else "",
            "place": tds[3] if len(tds) > 3 else "",
            "result": tds[4] if len(tds) > 4 else "",
            "grounds": tds[5] if len(tds) > 5 else "",
            "note": tds[6] if len(tds) > 6 else "",
            "placement": tds[7] if len(tds) > 7 else "",
        })
    return out


def parse_live_participants(card_html: str) -> list[dict[str, str]]:
    """Parse УЧАСТНИКИ from a live case-card.

    Row format: role | name | INN | KPP | OGRN | OGRNIP.
    GAS masks names as 'Информация скрыта' in some cards — such
    rows are returned with an empty name (masked=True marker).
    """
    out: list[dict[str, str]] = []
    m = re.search(r"Вид лица, участвующего в деле</b></td>(.*?)</table>",
                  card_html, re.S)
    if not m:
        return out
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        tds = [re.sub(r"<[^>]+>", "", td).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) < 2 or not tds[0]:
            continue
        masked = tds[1].startswith("Информация скрыта")
        out.append({
            "role": tds[0],
            "name": "" if masked else tds[1],
            "inn": tds[2] if len(tds) > 2 else "",
            "ogrn": tds[4] if len(tds) > 4 else "",
            "masked": "true" if masked else "false",
        })
    return out


def parse_live_card_header(card_html: str) -> dict[str, str]:
    """Parse the ДЕЛО header (УИД, категория, судья, результат)."""
    out: dict[str, str] = {}
    labels = ("Уникальный идентификатор дела", "Дата поступления",
              "Категория дела", "Судья", "Дата рассмотрения",
              "Результат рассмотрения")
    for label in labels:
        m = re.search(
            rf"<b>{label}</b>\s*</td>\s*<td[^>]*>(.*?)</td>",
            card_html, re.S)
        if m:
            val = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            out[label] = val
    return out


def probe_host(host: str, *, proxy: str | None = None,
               timeout: int = 20) -> dict[str, Any] | None:
    """Probe one candidate host: is it a live sud_delo module?"""
    try:
        code, body = _http_get(
            f"https://{host}/modules.php?name=sud_delo",
            proxy=proxy, timeout=timeout)
    except Exception:
        return None
    if code != 200 or len(body) < 30000 or "sud_delo" not in body:
        return None
    t = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
    title = t.group(1).strip()[:120] if t else ""
    n = sum(1 for _ in _ROW_RE.finditer(body))
    return {"cases": n, "title": title}


def grow_registry(candidates: list[str], *, proxy: str | None = None,
                  out_path: Path | None = None) -> dict[str, Any]:
    """Probe candidate hosts and merge live ones into the registry."""
    reg = load_registry()
    known = set(reg.get("oblsud", {})) | set(reg.get("district", {})) | \
        set(reg.get("supreme", {}))
    added = {}
    for host in candidates:
        if host in known:
            continue
        info = probe_host(host, proxy=proxy)
        if info:
            group = ("supreme" if host.startswith("vs")
                     else "oblsud" if host.startswith("oblsud")
                     else "district")
            reg.setdefault(group, {})[host] = {
                "cases_seen": info["cases"], "title": info["title"]}
            added[host] = info["cases"]
    if added:
        target = out_path or _REGISTRY_PATH
        target.write_text(json.dumps(reg, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    reg["_last_growth"] = {"added": added}
    return reg


def catalog_search(pattern: str = "", vnkod: str = "",
                   court_type: str = "") -> list[dict[str, Any]]:
    """Search the bundled full GAS court catalog (2335 courts).

    ``pattern`` — substring of court name (case-insensitive);
    ``vnkod`` — exact court code (e.g. ``50RS0001``);
    ``court_type`` — filter by type (RS/OS/GV/UD/...).
    Returns list of {vnkod, type, name, adress, coord}.
    """
    reg = load_registry()
    catalog: dict[str, Any] = reg.get("catalog", {})
    out = []
    pat = pattern.strip().lower()
    for code, info in catalog.items():
        if vnkod and code != vnkod:
            continue
        if court_type and not code[2:4] == court_type:
            continue
        if pat and pat not in info.get("name", "").lower():
            continue
        out.append({"vnkod": code, "type": code[2:4], **info})
    return out


def get_live_case_by_vnkod(vnkod: str, case_number: str, *,
                           date_window: int = 5,
                           explicit_dates: list[str] | None = None,
                           proxy: str | None = None,
                           timeout: int = 30,
                           use_cache: bool = True,
                           max_workers: int = 4) -> dict[str, Any]:
    """Live pipeline keyed by vnkod (from the bundled catalog).

    Resolves the court host from registry ``hosts[vnkod].host``,
    scans listings for ``case_number``, fetches the card. Works for
    ANY of the 2366 mapped courts (95% of GAS), not just the 22
    pre-verified hosts.
    """
    reg = load_registry()
    info = reg.get("hosts", {}).get(vnkod)
    if not info or not info.get("host"):
        raise SudrfLiveError(f"vnkod {vnkod!r}: no host in registry")
    host = info["host"]
    rows = lookup_case_number(case_number, host=host,
                              date_window=date_window,
                              explicit_dates=explicit_dates,
                              proxy=proxy, timeout=timeout,
                              use_cache=use_cache,
                              max_workers=max_workers)
    if not rows:
        scanned = f"{date_window} days" + (
            f" + {len(explicit_dates)} explicit dates"
            if explicit_dates else "")
        raise SudrfLiveError(
            f"case {case_number!r} not found in listings of {host} "
            f"(scanned {scanned})")
    row = rows[0]
    card = fetch_case_card(host, row.case_id, row.case_uid, proxy=proxy,
                           timeout=timeout, use_cache=use_cache)
    header = parse_live_card_header(card)
    return {
        "case_number": row.case_number,
        "case_id": row.case_id,
        "case_uid": row.case_uid,
        "card_url": row.card_url,
        "host": host,
        "court_name": reg["catalog"].get(vnkod, {}).get("name", ""),
        "uid": header.get("Уникальный идентификатор дела", ""),
        "header": header,
        "motion": parse_live_motion(card),
        "participants": parse_live_participants(card),
        "parser_version": SUDRF_LIVE_VERSION,
    }


__all__ = ["SudrfLiveError", "LiveListingRow", "get_live_case_by_vnkod",
           "catalog_search",
           "probe_host",
           "grow_registry", "parse_live_motion",
           "parse_live_participants", "parse_live_card_header",
           "get_live_case", "load_registry",
           "resolve_host", "fetch_listing", "fetch_case_card",
           "lookup_case_number", "list_registry_hosts",
           "sudrf_result_to_snapshot",
           "SUDRF_LIVE_VERSION"]
