"""KAD saved-HTML import (Ctrl+S) — parse a saved case card page.

Alternative to the EXTRACT_EXPRESSION JSON flow (colleague-legal feedback
1.2): a lawyer saves the KAD card page from the browser (Ctrl+S ->
HTML) and imports the file directly. Uses the SAME live-verified
selectors as the browser extract, applied to the saved DOM.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import (EmptyPayloadError, MalformedPayloadError,
                      WafDetectedError)
from ..normalize_refs import refs_match
from ..sources.base import RawSourceResult


def _visible_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", text)


def _el_text(html: str, tag: str, cls: str) -> str:
    """innerText of first <tag class~=cls>...</tag> (non-greedy)."""
    m = re.search(
        r"<" + tag + r"[^>]*class=\"[^\"]*\b" + re.escape(cls) +
        r"\b[^\"]*\"[^>]*>(.*?)</" + tag + r">",
        html, re.S | re.I)
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


def _td_list(html: str, cls: str) -> list[str]:
    """Party names inside <td class~=cls> of the parties table.

    Party rows use compound classes (td.plaintiffs.first); the header
    row has no li. Inside each li the party NAME is the first <a>
    text; hidden rollover spans carry the address and are skipped
    (PII policy).
    """
    names: list[str] = []
    for td_m in re.finditer(
            r"<td[^>]*class=\"([^\"]*)\"[^>]*>(.*?)</td>",
            html, re.S | re.I):
        classes = td_m.group(1).split()
        if cls not in classes:
            continue
        for li in re.finditer(r"<li[^>]*>(.*?)</li>", td_m.group(2),
                              re.S):
            inner = li.group(1)
            a = re.search(r"<a[^>]*>(.*?)</a>", inner, re.S)
            raw = a.group(1) if a else inner
            text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
            if text and len(text) > 1:
                names.append(text)
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out[:20]


def _judge(html: str) -> str:
    """Judge = td.first WITHOUT 'plaintiffs' in its class."""
    for td_m in re.finditer(
            r"<td[^>]*class=\"([^\"]*)\"[^>]*>(.*?)</td>",
            html, re.S | re.I):
        classes = td_m.group(1).split()
        if "first" in classes and "plaintiffs" not in classes:
            return " ".join(
                re.sub(r"<[^>]+>", " ", td_m.group(2)).split())
    return ""


def parse_kad_saved_html(html: str, *, case_ref: str, source: str,
                         source_url: str, retrieved_at: str,
                         raw_artifact_hash: str) -> dict:
    """Parse a saved KAD card HTML into an EXTRACT_EXPRESSION-shaped
    dict; downstream validation is shared with the live path."""
    from .parser import detect_kad_challenge

    if not html or not html.strip():
        raise EmptyPayloadError(source)
    # Challenge detection on VISIBLE text only: saved page HTML always
    # contains script/attribute strings mentioning captcha, which are
    # not a challenge page.
    if detect_kad_challenge(_visible_text(html)):
        raise WafDetectedError(source, 200)

    data = {
        "case_number": _el_text(html, "div", "js-case-header-case_num")
        or _el_text(html, "strong", "b-case-instance-number"),
        "status": _el_text(html, "div", "b-case-header-desc"),
        "court": _el_text(html, "td", "arbitration"),
        "judge": _judge(html),
        "plaintiffs": _td_list(html, "plaintiffs"),
        "respondents": _td_list(html, "defendants"),
        "third_parties": _td_list(html, "third"),
        "other": _td_list(html, "others"),
        "next_hearing": _el_text(html, "div", "b-instanceAdditional"),
        "duration": _el_text(html, "div", "case-dur"),
        "decision_date": _el_text(html, "a", "case-date")
        or _el_text(html, "div", "case-date"),
        "instance_id": "",
        "events": _events(html),
        "instances": _instances(html),
        "full_text": " ".join(_visible_text(html).split())[:10000],
    }
    return data


def _events(html: str) -> list[dict]:
    """Chronology events from saved card (same fields as live extract)."""
    events: list[dict] = []
    for item in re.finditer(
            r"<div[^>]*class=\"b-chrono-item js-chrono-item[^\"]*\""
            r"[^>]*>(.*?)</div>\s*(?=<div[^>]*class=\"b-chrono-item|"
            r"<div[^>]*class=\"js-chrono-item-status|$)",
            html, re.S | re.I):
        block = item.group(1)
        date = _el_text(block, "p", "case-date")
        if not date:
            dm = re.search(r'data-date="([^"]+)"', item.group(0))
            date = dm.group(1) if dm else ""
        events.append({
            "date": date,
            "type": _el_text(block, "p", "case-type"),
            "subject": _el_text(block, "p", "case-subject"),
            "result": _el_text(block, "h2", "b-case-result")
            or _el_text(block, "span", "js-case-result-text"),
            "pdf": (re.search(
                r'href="([^"]*PdfDocument[^"]*)"', block) or [None, ""])[1],
            "published": "b-case-publish_info" in block,
        })
    return events[:50]


def _instances(html: str) -> list[dict]:
    """Instance blocks (level, court, instance_id) from saved card."""
    out: list[dict] = []
    for h in re.finditer(
            r"<div[^>]*class=\"b-chrono-item-header[^\"]*\"(.*?)"
            r"class=\"b-collapse", html, re.S | re.I):
        block = h.group(1)
        iid = re.search(r'class="js-instanceId"\s+value="([^"]*)"', block)
        out.append({
            "level": _el_text(block, "strong", "")
            or " ".join(_el_text(block, "div", "l-col").split()[:2]),
            "court": _el_text(block, "span", "instantion-name"),
            "instance_id": iid.group(1) if iid else "",
        })
    # Skip JS-template stubs ('${session.SessionDate}') that survive in
    # saved HTML: only blocks with a real instance_id or court keep.
    return [i for i in out
            if (i["instance_id"] or i["court"])
            and "$" not in i["level"]]


class KadHtmlImportProvider:
    """Local import adapter: one saved KAD card HTML file."""

    name = "kad-html-import"
    media_type = "text/html"
    accepts_fixtures = False

    def __init__(self, html_path: Path | str, case_ref: str):
        self.html_path = Path(html_path)
        self.case_ref = case_ref
        # Filled by the last successful parse; agent-facing envelope
        # reads this for the instances[] extension.
        self.last_instances: list[dict] = []
        self.last_events: list[dict] = []

    def lookup(self, case_ref: str) -> RawSourceResult:
        from ..errors import SourceUnavailableError
        if not refs_match(case_ref, self.case_ref):
            raise SourceUnavailableError(
                self.name,
                f"requested '{case_ref}' but import was staged for "
                f"'{self.case_ref}'")
        if not self.html_path.is_file():
            raise SourceUnavailableError(
                self.name, f"import file missing: {self.html_path}")
        body = self.html_path.read_text(encoding="utf-8", errors="replace")
        return RawSourceResult(
            source=f"{self.name}:{self.html_path.name}",
            source_url=f"file://{self.html_path.resolve()}",
            status_code=200,
            body=body,
            media_type="text/html",
        )


def fetch_kad_saved_html(provider, case_ref: str,
                        retrieved_at: str) -> FetchOutcome:
    """lookup -> parse-as-extract -> shared parse pipeline."""
    from ..errors import CaseSearchError
    from ..pipeline import DEGRADED, ERROR, OK, FetchOutcome
    from ..schema import compute_raw_hash
    from .parser import parse_kad_data
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    if raw.media_type != "text/html":
        return FetchOutcome(ERROR, None,
                            "KAD HTML pipeline requires text/html")
    raw_hash = compute_raw_hash(raw.body)
    try:
        data = parse_kad_saved_html(
            raw.body, case_ref=case_ref, source=raw.source,
            source_url=raw.source_url, retrieved_at=retrieved_at,
            raw_artifact_hash=raw_hash)
        # Agent-facing extensions (cli envelope reads these).
        provider.last_instances = list(data.get("instances", []))
        provider.last_events = list(data.get("events", []))
        record = parse_kad_data(data, source=raw.source,
                                source_url=raw.source_url,
                                retrieved_at=retrieved_at,
                                raw_artifact_hash=raw_hash)
        if not refs_match(record.case_number, case_ref):
            raise MalformedPayloadError(
                raw.source, f"case identity mismatch: requested "
                            f"'{case_ref}', card reports "
                            f"'{record.case_number}'")
        snapshot = record.to_case_snapshot()
    except WafDetectedError as exc:
        return FetchOutcome(DEGRADED, None, str(exc), exc.retry_after)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    return FetchOutcome(OK, snapshot, "normalized")


__all__ = ["KadHtmlImportProvider", "parse_kad_saved_html",
           "fetch_kad_saved_html"]
