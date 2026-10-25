"""KAD UI-search via injected Camofox session (honest browser path).

Verified live 2026-09-10: KAD search works WITHOUT captcha when driven
as a real browser user — fill the case-number input on kad.arbitr.ru,
click the div.b-button «Найти», wait for AJAX results in
``table#b-cases tbody tr``. Direct API calls (POST /Kad/SearchInstances)
are blocked by pravocaptcha even inside Camofox — never use them.

This module contains NO network code and NO browser code: the caller
injects a ``browser_ops`` object with three callbacks (see KadBrowserOps).
A default Camofox implementation lives in cli.py (_kad_browser_ops).

Result rows carry the UUID kad_url — the key that unlocks the full
case card (instances, chronology, PDF links) via KadHtmlImportProvider/
live card parsing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..errors import ValidationError
from ..normalize_refs import normalize_case_ref

KAD_SEARCH_VERSION = "kad-search-0.1.0"

KAD_BASE = "https://kad.arbitr.ru"
NUMBER_INPUT_PLACEHOLDER = "например, А50-5568/08"

# Result row selectors (verified live against #b-cases tbody tr)
_ROW_SEL = "#b-cases tbody tr"


class KadSearchError(Exception):
    """Raised when the UI-search flow fails (form not ready, no
    results, unexpected DOM)."""


@dataclass(frozen=True)
class KadBrowserOps:
    """Injected browser operations (JS-capable, real user emulation).

    navigate(url)          — open a page, wait for load
    evaluate(js) -> str    — run JS in page context, return JSON string
    wait_seconds(seconds)  — sleep helper (injected to allow fast tests)
    """

    navigate: Callable[[str], None]
    evaluate: Callable[[str], str]
    wait_seconds: Callable[[float], None]


@dataclass
class KadSearchRow:
    case_number: str
    kad_url: str
    uuid: str
    judge: str
    court: str
    plaintiff: str
    respondent: str
    date: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_number": self.case_number,
            "kad_url": self.kad_url,
            "uuid": self.uuid,
            "judge": self.judge,
            "court": self.court,
            "plaintiff": self.plaintiff,
            "respondent": self.respondent,
            "date": self.date,
        }


@dataclass
class KadSearchResult:
    case_number: str
    rows: list[KadSearchRow] = field(default_factory=list)
    source_url: str = KAD_BASE
    parser_version: str = KAD_SEARCH_VERSION

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_number": self.case_number,
            "count": len(self.rows),
            "results": [r.to_dict() for r in self.rows],
            "source_url": self.source_url,
            "parser_version": self.parser_version,
        }


def _wait_for(
    ops: KadBrowserOps, js_condition: str, *,
    attempts: int = 15, delay: float = 2.0) -> bool:
    for _ in range(attempts):
        try:
            if ops.evaluate(js_condition) in ("true", "True", True):
                return True
        except Exception:
            pass
        ops.wait_seconds(delay)
    return False


def search_kad(
    case_number: str,
    ops: KadBrowserOps,
    *,
    wait_results: float = 3.0,
    result_attempts: int = 10,
) -> KadSearchResult:
    """Search KAD by case number via the real UI (fill → click → parse).

    Fail-closed: raises KadSearchError when the form never becomes
    ready, the search button is missing, or captcha blocks the flow.
    """
    if not case_number or not case_number.strip():
        raise ValueError("case_number must be non-empty")
    case_number = case_number.strip()

    ops.navigate(f"{KAD_BASE}/")

    # Wait for the SPA form (input with the known placeholder)
    ready = _wait_for(
        ops,
        f"!!document.querySelector('input[placeholder=\"{NUMBER_INPUT_PLACEHOLDER}\"]')",
    )
    if not ready:
        raise KadSearchError(
            "KAD search form did not become ready (SPA load timeout or "
            "blocked page)")

    # Fill the number input + click «Найти» (div.b-button)
    js_fill = (
        "(() => {"
        f"const inp = document.querySelector('input[placeholder=\"{NUMBER_INPUT_PLACEHOLDER}\"]');"
        "inp.focus();"
        f"inp.value = '{case_number}';"
        "inp.dispatchEvent(new Event('input', {bubbles: true}));"
        "const btn = Array.from(document.querySelectorAll('div.b-button'))"
        ".find(b => /найти/i.test(b.innerText));"
        "if (!btn) return 'NO_BUTTON';"
        "btn.click();"
        "return 'clicked';"
        "})()"
    )
    res = ops.evaluate(js_fill)
    if res == "NO_BUTTON":
        raise KadSearchError("KAD search button («Найти») not found")

    # Wait for AJAX results: either rows appear or captcha is shown
    captcha_seen = False
    rows_json = "[]"
    for _ in range(result_attempts):
        ops.wait_seconds(wait_results)
        try:
            raw = ops.evaluate(
                "JSON.stringify({rows: Array.from("
                f"document.querySelectorAll('{_ROW_SEL}')).map(tr => ({{"
                "case_number: tr.querySelector('a.num_case')?.innerText?.trim() || '',"
                "kad_url: tr.querySelector('a.num_case')?.href || '',"
                "judge: tr.querySelector('td.court .judge')?.innerText?.trim() || '',"
                "court: (tr.querySelector('td.court')?.innerText || '').replace(/\\s+/g,' ').trim(),"
                "plaintiff: (tr.querySelector('td.plaintiff')?.innerText || '').replace(/\\s+/g,' ').trim(),"
                "respondent: (tr.querySelector('td.respondent')?.innerText || '').replace(/\\s+/g,' ').trim(),"
                "date: tr.querySelector('.civil span')?.innerText?.trim() || ''"
                "})),"
                "captcha: /капч|captcha|Подтвердите/i.test(document.body.innerText)"
                "})")
            data = raw if isinstance(raw, dict) else json_loads(raw)
            if not isinstance(data, dict):
                continue
            if data.get("captcha"):
                captcha_seen = True
                break
            if data.get("rows"):
                rows_json = raw if isinstance(raw, str) else \
                    json.dumps(data, ensure_ascii=False)
                break
        except Exception:
            continue
    if captcha_seen:
        raise KadSearchError("KAD showed captcha during search")

    rows_data = json_loads(rows_json) if rows_json else []
    if isinstance(rows_data, dict):
        rows_raw = rows_data.get("rows", [])
    else:
        rows_raw = []  # "[]" from exhausted queue — no results
    if not rows_raw:
        raise KadSearchError(
            "no search results (case may be unpublished, or the number "
            "is wrong, or KAD returned an empty page)")

    rows: list[KadSearchRow] = []
    for r in rows_raw:
        kad_url = r.get("kad_url", "")
        uuid_m = re.search(r"/Card/([0-9a-f-]{36})", kad_url, re.I)
        rows.append(KadSearchRow(
            case_number=r.get("case_number", ""),
            kad_url=kad_url,
            uuid=uuid_m.group(1) if uuid_m else "",
            judge=r.get("judge", ""),
            court=r.get("court", ""),
            plaintiff=r.get("plaintiff", ""),
            respondent=r.get("respondent", ""),
            date=r.get("date", ""),
        ))

    return KadSearchResult(case_number=case_number, rows=rows)


def json_loads(text: str) -> Any:
    return json.loads(text)


def kad_case_history(
    case_number: str,
    ops: KadBrowserOps,
    *,
    chrono_wait: float = 4.0,
    card_wait: float = 6.0,
    extract_expression: str = "",
) -> dict[str, Any]:
    """Судьба дела одним вызовом: номер → UUID → карточка КАД →
    все инстанции + хронология + события.

    Flow (verified live 2026-09-10 on А19-30387/2025):
    1. search_kad() finds the case UUID via UI search (no captcha);
    2. navigates to /Card/<UUID>;
    3. expands chronology spoilers (.js-collapse);
    4. runs EXTRACT_EXPRESSION (caller passes it to avoid circular
       imports) and returns the full extract + uuid chain.

    Returns dict with keys: search (rows), card (extract data),
    case_number (canonical from card), instances.
    """
    from .parser import EXTRACT_EXPRESSION as _DEFAULT_EXPR

    expr = extract_expression or _DEFAULT_EXPR
    res = search_kad(case_number, ops)
    if not res.ok:
        raise KadSearchError(
            f"case '{case_number}' not found in KAD search")
    row = res.rows[0]

    ops.navigate(row.kad_url)
    ops.wait_seconds(card_wait)
    # expand chronology spoilers
    ops.evaluate(
        "Array.from(document.querySelectorAll('.js-collapse'))"
        ".forEach(b => b.click()); 'ok'")
    ops.wait_seconds(chrono_wait)

    raw = ops.evaluate(expr)
    card = raw if isinstance(raw, dict) else json_loads(raw)
    if not isinstance(card, dict):
        raise KadSearchError(
            "card extract returned non-object (raw page changed?)")

    return {
        "search": res.to_dict(),
        "kad_uuid": row.uuid,
        "kad_url": row.kad_url,
        "case_number": card.get("case_number", case_number),
        "card": card,
        "instances": card.get("instances", []),
        "events": card.get("events", []),
        "parser_version": KAD_SEARCH_VERSION,
    }


# ---------------------------------------------------------------------------
# Participant search (by INN / OGRN / name)
# ---------------------------------------------------------------------------

KAD_PARTICIPANT_VERSION = "kad-participant-0.1.0"

_PARTICIPANT_TAB_MARKER = "участник"
_PARTICIPANT_SEARCH_BUTTON_TEXT = "найти"

_PARTICIPANT_FILL_JS_TEMPLATE = r"""
(() => {{
  function findInput(patterns) {{
    const inputs = Array.from(document.querySelectorAll('input, textarea'));
    for (const inp of inputs) {{
      const text = (inp.placeholder || inp.getAttribute('placeholder') ||
                    inp.title || inp.name || '').toLowerCase();
      for (const p of patterns) {{
        if (text.indexOf(p) !== -1) return inp;
      }}
    }}
    return null;
  }}
  const tab = Array.from(document.querySelectorAll('a, li, span, label, button, div'))
    .find(el => /{tab_marker}/i.test(el.innerText || ''));
  if (!tab) return JSON.stringify({{error: 'NO_PARTICIPANT_TAB'}});
  tab.click();
  let err = null;
  const setField = (val, patterns, name) => {{
    if (!val) return true;
    const inp = findInput(patterns);
    if (!inp) {{ err = name; return false; }}
    inp.focus();
    inp.value = val;
    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
    return true;
  }};
  if (!setField({inn_s}, ['инн'], 'NO_INN_INPUT')) {{}}
  if (err) return JSON.stringify({{error: err}});
  if (!setField({ogrn_s}, ['огрн', 'огрнип'], 'NO_OGRN_INPUT')) {{}}
  if (err) return JSON.stringify({{error: err}});
  if (!setField({name_s}, ['наименован', 'фио', 'участник', 'организаци', 'name'],
                'NO_NAME_INPUT')) {{}}
  if (err) return JSON.stringify({{error: err}});
  const btn = Array.from(document.querySelectorAll('div.b-button, button, input[type="submit"]'))
    .find(b => /{button_text}/i.test(b.innerText || b.value || ''));
  if (!btn) return JSON.stringify({{error: 'NO_SEARCH_BUTTON'}});
  btn.click();
  return JSON.stringify({{status: 'clicked'}});
}})()
"""

_PARTICIPANT_EXTRACT_JS = r"""
JSON.stringify({
  rows: Array.from(document.querySelectorAll('#b-cases tbody tr')).map(tr => ({
    case_number: tr.querySelector('a.num_case')?.innerText?.trim() || '',
    kad_url: tr.querySelector('a.num_case')?.href || '',
    judge: tr.querySelector('td.court .judge')?.innerText?.trim() || '',
    court: (tr.querySelector('td.court')?.innerText || '').replace(/\s+/g, ' ').trim(),
    plaintiff: (tr.querySelector('td.plaintiff')?.innerText || '').replace(/\s+/g, ' ').trim(),
    respondent: (tr.querySelector('td.respondent')?.innerText || '').replace(/\s+/g, ' ').trim(),
    date: tr.querySelector('.civil span')?.innerText?.trim() || ''
  })),
  captcha: /капч|captcha|Подтвердите/i.test(document.body.innerText),
  waf: /pravocaptcha|Доступ заблокирован|Подтверждение действия/i.test(document.body.innerText),
  sample: document.body.innerText.substring(0, 120)
})
"""


def _js_str(value: Any) -> str:
    """Safely embed a Python string (or None) into a JS literal."""
    return json.dumps(value if value is not None else "")


@dataclass
class KadParticipantHit:
    """One case found by a participant search."""

    case_number: str
    court: str
    judge: str
    parties: list[str]
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge,
            "parties": list(self.parties),
            "url": self.url,
        }


@dataclass
class KadParticipantResult:
    """Fail-closed outcome of a KAD participant search."""

    status: str                       # "ok" | "degraded" | "error"
    reason: str
    hits: list[KadParticipantHit]
    query: dict[str, Any]
    page: int
    source_url: str = KAD_BASE
    parser_version: str = KAD_PARTICIPANT_VERSION
    retrieved_at: str = ""
    has_more: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.hits)

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "parser_version": self.parser_version,
            "retrieved_at": self.retrieved_at,
            "query": dict(self.query),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "count": len(self.hits),
            "hits": [h.to_dict() for h in self.hits],
            "page": self.page,
            "has_more": self.has_more,
            "provenance": self.provenance,
        }


def _build_participant_query(
    inn: str | None,
    ogrn: str | None,
    name: str | None,
    page: int,
) -> dict[str, Any]:
    """Validate and normalize participant query parameters."""
    query: dict[str, Any] = {"page": page}
    inn_s = (inn or "").strip()
    ogrn_s = (ogrn or "").strip()
    name_s = (name or "").strip()
    if inn_s:
        query["inn"] = inn_s
    if ogrn_s:
        query["ogrn"] = ogrn_s
    if name_s:
        query["name"] = name_s
    if not any(k in query for k in ("inn", "ogrn", "name")):
        raise ValidationError(
            "kad-search", "at least one of inn, ogrn or name is required")
    if page < 1:
        raise ValidationError("kad-search", "page must be >= 1")
    return query


def _safe_evaluate_json(ops: KadBrowserOps, js: str) -> dict[str, Any] | None:
    try:
        raw = ops.evaluate(js)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            return json_loads(raw)
    except Exception:
        pass
    return None


def _participant_degraded(
    code: str,
    detail: str,
    query: dict[str, Any],
    page: int,
    *,
    retrieved_at: str = "",
) -> KadParticipantResult:
    return KadParticipantResult(
        status="degraded",
        reason=f"{code}: {detail}",
        hits=[],
        query=query,
        page=page,
        retrieved_at=retrieved_at,
    )


def kad_search(
    inn: str | None = None,
    ogrn: str | None = None,
    name: str | None = None,
    *,
    page: int = 1,
    ops: KadBrowserOps | None = None,
    wait_results: float = 3.0,
    result_attempts: int = 10,
    retrieved_at: str | None = None,
) -> KadParticipantResult:
    """Search KAD by participant (INN / OGRN / name) via the real UI.

    Fail-closed: returns a ``KadParticipantResult`` with ``status`` set to
    ``degraded`` when the form is not ready, a captcha/WAF challenge is
    shown, or the response cannot be parsed. Never fabricates results.
    """
    query = _build_participant_query(inn, ogrn, name, page)
    if ops is None:
        raise ValueError("kad_search requires an injected browser ops object")
    retrieved = retrieved_at or ""

    ops.navigate(f"{KAD_BASE}/")

    ready_js = (
        f"!!document.querySelector('input[placeholder=\"{NUMBER_INPUT_PLACEHOLDER}\"]')"
    )
    if not _wait_for(ops, ready_js, attempts=15, delay=2.0):
        return _participant_degraded(
            "FORM_NOT_READY",
            "KAD search form did not become ready (SPA load timeout or blocked)",
            query, page, retrieved_at=retrieved)

    fill_js = _PARTICIPANT_FILL_JS_TEMPLATE.format(
        tab_marker=_PARTICIPANT_TAB_MARKER,
        button_text=_PARTICIPANT_SEARCH_BUTTON_TEXT,
        inn_s=_js_str(query.get("inn")),
        ogrn_s=_js_str(query.get("ogrn")),
        name_s=_js_str(query.get("name")),
    )
    fill_res = _safe_evaluate_json(ops, fill_js)
    if not isinstance(fill_res, dict):
        return _participant_degraded(
            "UNEXPECTED_RESPONSE",
            "participant search form interaction returned non-JSON",
            query, page, retrieved_at=retrieved)
    if fill_res.get("error"):
        return _participant_degraded(
            "UI_ERROR",
            fill_res["error"],
            query, page, retrieved_at=retrieved)
    if fill_res.get("status") != "clicked":
        return _participant_degraded(
            "UNEXPECTED_RESPONSE",
            "participant search button was not clicked",
            query, page, retrieved_at=retrieved)

    rows_raw: list[dict[str, Any]] = []
    for _ in range(result_attempts):
        ops.wait_seconds(wait_results)
        data = _safe_evaluate_json(ops, _PARTICIPANT_EXTRACT_JS)
        if not isinstance(data, dict):
            continue
        if data.get("waf"):
            return _participant_degraded(
                "WAF_BLOCKED",
                "KAD showed a WAF/captcha challenge",
                query, page, retrieved_at=retrieved)
        if data.get("captcha"):
            return _participant_degraded(
                "PRACTICE_CAPTCHA",
                "KAD showed a captcha during participant search",
                query, page, retrieved_at=retrieved)
        rows_raw = data.get("rows") or []
        if rows_raw:
            break

    if not rows_raw:
        return _participant_degraded(
            "PRACTICE_EMPTY",
            "no participant search results (empty response or changed DOM)",
            query, page, retrieved_at=retrieved)

    hits: list[KadParticipantHit] = []
    for r in rows_raw:
        if not isinstance(r, dict):
            continue
        parties = [
            r.get("plaintiff", "").strip(),
            r.get("respondent", "").strip(),
        ]
        parties = [p for p in parties if p]
        case_number = normalize_case_ref(r.get("case_number", ""))
        hits.append(KadParticipantHit(
            case_number=case_number,
            court=r.get("court", "").strip(),
            judge=r.get("judge", "").strip(),
            parties=parties,
            url=r.get("kad_url", "").strip(),
        ))

    return KadParticipantResult(
        status="ok",
        reason="search-ok",
        hits=hits,
        query=query,
        page=page,
        retrieved_at=retrieved,
    )


__all__ = ["KadBrowserOps", "KadSearchRow", "KadSearchResult",
           "KadSearchError", "search_kad", "kad_case_history",
           "KAD_SEARCH_VERSION", "kad_search", "KadParticipantHit",
           "KadParticipantResult", "KAD_PARTICIPANT_VERSION"]
