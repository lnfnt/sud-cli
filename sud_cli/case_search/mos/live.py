"""mos-gorsud.ru live adapter: case search + card parsing (SSR).

Verified live 2026-09-15 (Хамовнический районный суд, 02-2101/2026):
- search: GET https://mos-gorsud.ru/rs/{court}/search?query=<number>
  -> SSR HTML with card links:
  /rs/{court}/services/cases/{kind}/details/<uuid>
  (kind: civil|kao|adm|criminal)
- card:   GET .../details/<uuid> -> ~890KB SSR HTML:
  Стороны (Истец/Ответчик), Судья, Текущее состояние,
  Движение дела (История состояний table), Документы table.

No JS needed: all data is server-rendered. Fail-closed fail-closed by design.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler

from case_search.errors import MalformedPayloadError
from case_search.normalize import normalize
from case_search.schema import CaseSnapshot
from case_search.mos import cache as _mos_cache

MOS_LIVE_VERSION = "mos-live-0.1.0"

BASE = "https://mos-gorsud.ru"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_CARD_LINK_RE = re.compile(
    r'href="(/rs/[^/"]+/services/cases/(?:civil|kao|adm|criminal)/'
    r'details/[0-9a-f-]{36})"')

KNOWN_COURTS = {
    # Existing entries (verified live) — keep unchanged.
    "horoshevskij": "Хорошевский районный суд",
    "hamovnichesky": "Хамовнический районный суд",
    "tverskoj": "Тверской районный суд",
    "presnensky": "Пресненский районный суд",
    "cheryomushki": "Черёмушкинский районный суд",
    "begovoj": "Беговой районный суд",
    "kuntsevsky": "Кунцевский районный суд",
    "gagarinsky": "Гагаринский районный суд",
    "lefortovo": "Лефортовский районный суд",
    "basmanny": "Басманный районный суд",
    "zamoskvorechye": "Замоскворецкий районный суд",
    "tagansky": "Таганский районный суд",
    "savelovsky": "Савёловский районный суд",
    "butyrsky": "Бутырский районный суд",
    "meshchansky": "Мещанский районный суд",
    # Expanded Moscow district courts (canonical slug -> Russian name).
    "preobrazhensky": "Преображенский районный суд",
    "veshnyaki": "Вешняковский районный суд",
    "novokosino": "Новокосинский районный суд",
    "kosino-uhtomsky": "Косино-Ухтомский районный суд",
    "ivanovskoe": "Ивановский районный суд",
    "sokolinaya-gora": "Сокольнический районный суд",
    "perovo": "Перовский районный суд",
    "izmaylovo": "Измайловский районный суд",
    "bogorodskoe": "Богородский районный суд",
    "golyanovo": "Гольяновский районный суд",
    "severnyj": "Северный районный суд",
    "otradnyj": "Отрадный районный суд",
    "marfino": "Марфинский районный суд",
    "timiryazevsky": "Тимирязевский районный суд",
    "beskudnikovsky": "Бескудниковский районный суд",
    "vostochno-degunino": "Восточно-Дегунинский районный суд",
    "zapadnoe-degunino": "Западно-Дегунинский районный суд",
    "levoberezhnyj": "Левобережный районный суд",
    "golovinsky": "Головинский районный суд",
    "voikovsky": "Войковский районный суд",
    "pokrovskoe-streshnevo": "Покровско-Стрешневский районный суд",
    "shchukinsky": "Щукинский районный суд",
    "mitino": "Митинский районный суд",
    "horoshevo-mnevniki": "Хорошёво-Мнёвниковский районный суд",
    "krylatskoe": "Крылатский районный суд",
    "strogino": "Строгинский районный суд",
    "ramenki": "Раменский районный суд",
    "lomonosovsky": "Ломоносовский районный суд",
    "obruchevsky": "Обручевский районный суд",
    "akademichesky": "Академический районный суд",
    "kotlovka": "Котловский районный суд",
    "yasenevo": "Ясеневский районный суд",
    "chertanovo": "Чертановский районный суд",
    "biryulevo": "Бирюлёвский районный суд",
    "orekhovo-borisovo": "Орехово-Борисовский районный суд",
    "zyablikovo": "Зябликовский районный суд",
    "tsaritsyno": "Царицынский районный суд",
    "danilovsky": "Даниловский районный суд",
    "donskoy": "Донской районный суд",
    "nagatino-sadovniki": "Нагатино-Садовниковский районный суд",
    "moskvoretsky": "Москворецкий районный суд",
    "yakimanka": "Якиманский районный суд",
    "arbat": "Арбатский районный суд",
}


def resolve_mos_slug(name_or_slug: str) -> str | None:
    """Resolve a user-typed court name or slug to a canonical mos-gorsud slug.

    Matching is case-insensitive. Exact slug match is tried first, then
    exact Russian name match, then substring match on the Russian name.
    Returns ``None`` when nothing matches.
    """
    if not name_or_slug or not str(name_or_slug).strip():
        return None
    query = str(name_or_slug).strip().lower()
    for slug, name in KNOWN_COURTS.items():
        if slug.lower() == query:
            return slug
    for slug, name in KNOWN_COURTS.items():
        if name.lower() == query:
            return slug
    for slug, name in KNOWN_COURTS.items():
        if query in name.lower():
            return slug
    return None


class MosLiveError(Exception):
    pass


_PUBLISHED_MARKERS = (
    "опубликовано", "изготовлено", "вынесено", "направлено",
    "вручено", "отправлено",
)


def _normalize_ws(text: str) -> str:
    """Strip and collapse whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def _is_published(event: str, document: str) -> bool:
    """True when the row carries a document or explicit publication text."""
    if document.strip():
        return True
    hay = (event + " " + document).casefold()
    return any(marker in hay for marker in _PUBLISHED_MARKERS)


def _extract_pdf_url(document: str) -> str:
    """Pull a PDF/URL out of the free-text document cell, if any."""
    doc = document.strip()
    if not doc:
        return ""
    # direct URL
    m = re.search(r"https?://\S+", doc)
    if m:
        return m.group(0)
    # absolute portal path
    m = re.search(r"/\S+\.pdf\S*", doc, re.I)
    if m:
        return BASE + m.group(0)
    # bare pdf filename
    m = re.search(r"\S+\.pdf\S*", doc, re.I)
    if m:
        return BASE + "/" + m.group(0)
    return ""


def canonize_mos_events(motion: list[dict]) -> dict[str, Any]:
    """Canonize mos-gorsud ``История состояний`` rows.

    Input rows have the mos-specific shape
    ``{"date": str, "event": str, "document": str}``.
    Returns a dict with:

    - ``events_timeline``: list of package-standard events
      ``{"date", "name", "published", "pdf_url"}``;
    - ``dropped_duplicates``: number of (date, name) duplicates removed;
    - ``warnings``: list of human-readable warnings (currently one per
      dropped duplicate).

    Fail-closed: rows missing ``date`` or ``event`` raise ``MosLiveError``.
    """
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    warnings: list[str] = []
    for idx, row in enumerate(motion):
        if not isinstance(row, dict):
            raise MosLiveError(
                f"motion row {idx} must be a dict, got {type(row).__name__}")
        date = str(row.get("date", "")).strip()
        event = str(row.get("event", "")).strip()
        document = str(row.get("document", "")).strip()
        if not date:
            raise MosLiveError(f"motion row {idx} missing date: {row}")
        if not event:
            raise MosLiveError(f"motion row {idx} missing event: {row}")
        name = _normalize_ws(event)
        key = (date, name)
        if key in seen:
            dropped += 1
            warnings.append(
                f"dropped duplicate timeline event at index {idx}: {key}")
            continue
        seen.add(key)
        events.append({
            "date": date,
            "name": name,
            "published": _is_published(event, document),
            "pdf_url": _extract_pdf_url(document),
        })
    return {
        "events_timeline": events,
        "dropped_duplicates": dropped,
        "warnings": warnings,
    }


_MOS_ROLE_MAP = {
    "истец": "plaintiff",
    "ответчик": "respondent",
    "третье лицо": "third_parties",
}


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mos_raw_hash(card: dict[str, Any]) -> str:
    raw_html = card.get("raw_html")
    if isinstance(raw_html, str) and raw_html.strip():
        return hashlib.sha256(raw_html.encode("utf-8")).hexdigest()
    blob = json.dumps(card, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def mos_card_to_snapshot(card: dict[str, Any],
                         *, warnings: list[str] | None = None) -> CaseSnapshot:
    """Build a canonical CaseSnapshot from a mos-gorsud card dict.

    The card dict is the output of :func:`get_mos_case` (or a compatible
    hand-built dict). Required keys: ``case_number_query`` and
    ``court_slug``. Fail-closed: missing required keys raise
    ``MalformedPayloadError``.
    """
    if not isinstance(card, dict):
        raise MalformedPayloadError("mos-gorsud", "card must be a dict")

    case_number = card.get("case_number_query")
    court_slug = card.get("court_slug")
    if not case_number:
        raise MalformedPayloadError(
            "mos-gorsud", "missing field 'case_number_query'")
    if not court_slug:
        raise MalformedPayloadError(
            "mos-gorsud", "missing field 'court_slug'")
    if court_slug not in KNOWN_COURTS:
        raise MalformedPayloadError(
            "mos-gorsud", f"unknown court_slug: {court_slug!r}")

    merged_warnings: list[str] = list(warnings or [])
    meta = card.get("events_timeline_meta") or {}
    if isinstance(meta, dict):
        merged_warnings.extend(meta.get("warnings", []))

    participants: dict[str, list[str]] = {
        "plaintiff": [], "respondent": [], "third_parties": []}
    for party in card.get("parties", []):
        if not isinstance(party, dict):
            continue
        role = str(party.get("role", "")).strip()
        name = str(party.get("name", "")).strip()
        if not role or not name:
            continue
        bucket = _MOS_ROLE_MAP.get(role.lower())
        if bucket is None:
            bucket = "third_parties"
            merged_warnings.append(
                f"unmapped party role '{role}' placed in third_parties")
        participants[bucket].append(name)

    events_timeline = list(card.get("events_timeline", []))
    last_event = None
    if events_timeline:
        first = events_timeline[0]
        if isinstance(first, dict):
            doc_url = str(first.get("pdf_url", "")).strip()
            if not doc_url:
                doc_url = str(card.get("card_url", "")).strip()
            if not doc_url:
                doc_url = "unknown"
            last_event = {
                "date": first.get("date", ""),
                "event_type": first.get("name", ""),
                "document_url": doc_url,
            }

    payload = {
        "case_number": str(case_number).strip(),
        "court": KNOWN_COURTS[court_slug] + " (mos-gorsud)",
        "judge": str(card.get("judge", "") or "unknown").strip(),
        "status": str(card.get("current_status", "") or "unknown").strip(),
        "participants": participants,
        "next_hearing": None,
        "last_event": last_event,
        "events_timeline": events_timeline,
        "updated_at": _utc_today_iso(),
    }

    return normalize(
        payload,
        source="mos-gorsud",
        source_url=str(card.get("card_url", "")),
        retrieved_at=_utc_now_iso(),
        raw_artifact_hash=_mos_raw_hash(card),
        warnings=tuple(merged_warnings),
        parser_version=MOS_LIVE_VERSION,
    )


def _http_get(url: str, *, proxy: str | None = None,
              timeout: int = 30) -> str:
    """netguard: fail-closed egress/breaker pre-check; 403/429 -> cooldown."""
    from sud_cli.sud_netguard import breaker_soft_fail, breaker_trip, check_before_fetch

    try:
        check_before_fetch(url)
    except Exception as exc:
        raise MosLiveError(f"netguard blocked {url}: {exc}") from exc
    handlers = []
    if proxy:
        handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
    opener = build_opener(*handlers)
    req = Request(url, headers={"User-Agent": _UA})
    from urllib.error import HTTPError
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code in (403, 429):
            breaker_trip(url, reason=f"http_{exc.code}")
        raise
    if "Доступ ограничен".casefold() in body.casefold():
        breaker_soft_fail(url, reason="challenge_marker")
    return body


def mos_search(court_slug: str, case_number: str, *,
               proxy: str | None = None, timeout: int = 30,
               use_cache: bool = True) -> list:
    """Search mos-gorsud for ``case_number`` in ``court_slug``.

    ``use_cache`` toggles the SQLite cache. Cache reads and writes are
    fail-closed: a cache error degrades to a direct HTTP fetch.

    Empty result sets are never served from cache (they are refetched)
    to avoid poisoning the cache while a case is still being indexed.
    """
    if use_cache:
        try:
            cached = _mos_cache.get_cached_search(court_slug, case_number)
            if cached is not None:
                return cached
        except Exception:
            pass

    url = BASE + "/rs/" + court_slug + "/search?query=" + quote(case_number)
    html = _http_get(url, proxy=proxy, timeout=timeout)
    if "не найдено" in html.lower() and "case" not in html.lower():
        results: list[dict[str, str]] = []
    else:
        links = _CARD_LINK_RE.findall(html)
        seen = set()
        results = []
        for path in links:
            if path in seen:
                continue
            seen.add(path)
            kind = re.search(r"cases/(\w+)/details", path).group(1)
            uid = path.rsplit("/", 1)[-1]
            results.append({"kind": kind, "uuid": uid, "card_url": BASE + path})

    if use_cache and results:
        try:
            _mos_cache.put_search(court_slug, case_number, results)
        except Exception:
            pass
    return results


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _tds(tr_html: str) -> list:
    return [re.sub(r"<[^>]+>", "", td).strip()
            for td in re.findall(r"<td[^>]*>(.*?)</td>", tr_html, re.S)]


def mos_card(court_slug: str, uuid: str, *, kind: str = "civil",
             proxy: str | None = None, timeout: int = 40,
             use_cache: bool = True) -> dict:
    """Fetch and parse a mos-gorsud case card.

    ``use_cache`` toggles the SQLite cache. Cache reads and writes are
    fail-closed: a cache error degrades to a direct HTTP fetch.
    """
    url = BASE + "/rs/" + court_slug + "/services/cases/" + kind + "/details/" + uuid

    if use_cache:
        try:
            cached_html = _mos_cache.get_cached_card(uuid)
            if cached_html is not None:
                html = cached_html
            else:
                html = _http_get(url, proxy=proxy, timeout=timeout)
                try:
                    _mos_cache.put_card(uuid, html)
                except Exception:
                    pass
        except Exception:
            html = _http_get(url, proxy=proxy, timeout=timeout)
    else:
        html = _http_get(url, proxy=proxy, timeout=timeout)

    result = {
        "court_slug": court_slug, "uuid": uuid, "kind": kind,
        "card_url": url, "parser_version": MOS_LIVE_VERSION,
    }

    m = re.search(r"Cудья\s*</[^>]+>.*?>\s*([^<]+?)\s*</", html, re.S)
    if m:
        result["judge"] = m.group(1).strip()
    else:
        m = re.search(r"Cудья\s*</td>\s*<td[^>]*>([^<]+)", html)
        result["judge"] = m.group(1).strip() if m else ""

    parties = []
    # Формат: <p class="table-bold-text">Роль</p>Имя<br/> (блок Стороны)
    for m in re.finditer(
            r'<p class="table-bold-text">(Истец|Ответчик|Третье лицо|'
            r'Представитель)</p>([^<]+)(?:<br/?\s*/?>|$)', html):
        role = m.group(1).strip()
        name = m.group(2).strip()
        name = (name.replace("&quot;", '"').replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
        if name:
            parties.append({"role": role, "name": name})
    result["parties"] = parties

    m = re.search(r"Текущее состояние\s*</div>\s*<div class=\"right\">\s*(.*?)</div>",
                  html, re.S)
    result["current_status"] = re.sub(r"\s+", " ",
                                      re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""

    m = re.search(r"История состояний</h3>.*?<tbody>(.*?)</tbody>", html, re.S)
    events = []
    if m:
        for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
            tds = _tds(tr)
            if len(tds) >= 2 and tds[0]:
                events.append({"date": tds[0],
                               "event": tds[1],
                               "document": tds[2] if len(tds) > 2 else ""})
    result["motion"] = events

    docs = []
    m3 = re.search(r"Документы из описи.*?<tbody>(.*?)</tbody>", html, re.S)
    if m3:
        for tr in re.findall(r"<tr>(.*?)</tr>", m3.group(1), re.S):
            tds = _tds(tr)
            pdf_m = re.search(r'href="([^"]*\.pdf[^"]*)"', tr)
            if len(tds) >= 2 and tds[0]:
                docs.append({"date": tds[0], "kind": tds[1],
                             "text": tds[2] if len(tds) > 2 else "",
                             "pdf_url": (BASE + pdf_m.group(1)) if pdf_m else ""})
    result["documents"] = docs
    return result


def get_mos_case(court_slug: str, case_number: str, *,
                 kind: str | None = None, proxy: str | None = None,
                 timeout: int = 30, use_cache: bool = True) -> dict:
    hits = mos_search(court_slug, case_number, proxy=proxy, timeout=timeout,
                      use_cache=use_cache)
    if not hits:
        raise MosLiveError(
            "case " + repr(case_number) + " not found on mos-gorsud "
            "(court: " + court_slug + ")")
    hit = hits[0]
    if kind:
        for h in hits:
            if h["kind"] == kind:
                hit = h
                break
    card = mos_card(court_slug, hit["uuid"], kind=hit["kind"],
                    proxy=proxy, timeout=timeout, use_cache=use_cache)
    card["search_hits"] = len(hits)
    card["case_number_query"] = case_number
    canonized = canonize_mos_events(card.get("motion", []))
    card["events_timeline"] = canonized["events_timeline"]
    card["events_timeline_meta"] = {
        "dropped_duplicates": canonized["dropped_duplicates"],
        "warnings": canonized["warnings"],
    }
    return card


__all__ = ["MosLiveError", "mos_search", "mos_card", "get_mos_case",
           "canonize_mos_events", "mos_card_to_snapshot", "KNOWN_COURTS",
           "MOS_LIVE_VERSION", "resolve_mos_slug"]
