"""Quote verification (S-52, v2.9.0) — правило «тексты только из реестра»: «никогда не ok на глаз».

Pure offline functions: a quoted fragment is checked against an explicit local
source — a bundled historical NPA edition (``sud_cli.npa_as_of``), the events
table of a case, or a plain-text file. The verdict is one of:

  exact               the normalized fragment occurs literally in the source;
  fuzzy               no literal match, but a difflib similarity above the
                      ``1 - max_fuzzy`` threshold (only when max_fuzzy > 0);
  not_found           neither of the above — the honest default;
  edition_unavailable the requested bundle edition/article/date is absent
                      (an NPA source NEVER degrades to not_found: absence of
                      the bundle is not absence of the norm).

Normalization is minimal and lossless for legal text: NBSP -> space, whitespace
runs -> single space, ё -> е, case-folded. Punctuation is deliberately NOT
stripped — dropping it is how a "looks the same" citation quietly becomes ok.
"""

import difflib
import re
from pathlib import Path

SNIPPET_WINDOW = 80
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_WS_RE = re.compile(r"\s+")


class VerifyInputError(ValueError):
    """Bad arguments for the verification itself (empty text, unknown source)."""


def normalize(text: str) -> str:
    """Whitespace-tolerant, ё/е-insensitive, lowercased; punctuation kept."""
    out = text.replace("\u00a0", " ").replace("\u202f", " ")
    out = _WS_RE.sub(" ", out).strip()
    return out.replace("\u0401", "\u0415").replace("\u0451", "\u0435").lower()


def _snippet(body: str, start: int, end: int) -> str:
    lo = max(0, start - SNIPPET_WINDOW)
    hi = min(len(body), end + SNIPPET_WINDOW)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(body) else ""
    return prefix + body[lo:hi] + suffix


def _best_window(query: str, body: str):
    """Best SequenceMatcher ratio over same-length-ish sliding windows."""
    if not query or not body:
        return 0.0, 0, 0
    length = len(query)
    lengths = sorted({w for w in range(max(1, length - 3), length + 4) if w <= len(body)})
    if not lengths:
        lengths = [len(body)]
    step = 1 if len(body) <= 20000 else 4
    best_ratio, best_start, best_len = 0.0, 0, lengths[0]
    for wlen in lengths:
        for i in range(0, len(body) - wlen + 1, step):
            ratio = difflib.SequenceMatcher(None, query, body[i:i + wlen]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start, best_len = ratio, i, wlen
                if ratio >= 1.0:
                    return best_ratio, best_start, best_len
    return best_ratio, best_start, best_len


def _match(norm_query: str, norm_body: str, max_fuzzy: float, location: str,
           extra=None):
    """Verdict for one normalized body. Exact only on a literal substring."""
    base = {"location": location, "offset": None, "matched_fragment": None,
            "ratio": None, "threshold": None, "snippet": None, "detail": None}
    if extra:
        base.update(extra)
    idx = norm_body.find(norm_query)
    if idx >= 0:
        return {**base, "verdict": "exact", "offset": idx,
                "matched_fragment": norm_body[idx:idx + len(norm_query)]}
    if max_fuzzy and max_fuzzy > 0:
        threshold = 1.0 - max_fuzzy
        ratio, start, wlen = _best_window(norm_query, norm_body)
        if ratio >= threshold:
            return {**base, "verdict": "fuzzy", "offset": start, "ratio": round(ratio, 4),
                    "threshold": round(threshold, 4),
                    "matched_fragment": norm_body[start:start + wlen] or None,
                    "snippet": _snippet(norm_body, start, start + wlen)}
        return {**base, "verdict": "not_found",
                "detail": f"best fuzzy ratio {round(ratio, 4)} below threshold {round(threshold, 4)}"}
    return {**base, "verdict": "not_found", "detail": "fuzzy matching disabled (max_fuzzy=0)"}


# ---------------------------------------------------------------- sources


def _source_npa(act: str, article: str, date: str, query: str, max_fuzzy: float):
    from .npa_as_of import lookup_as_of

    location = f"{act}/{article}/{date}"
    result = lookup_as_of(act, article, date)
    if result.get("status") != "ok":
        # Absent bundle/edition is a coverage gap, never a "not found" claim.
        return {"verdict": "edition_unavailable", "location": location, "offset": None,
                "matched_fragment": None, "ratio": None, "threshold": None,
                "snippet": None, "detail": result.get("detail", "edition not in offline bundle")}
    return _match(query, normalize(result["text"]), max_fuzzy, location)


def _source_case(case_id: str, state_dir, query: str, max_fuzzy: float):
    from .db import get_case_events, get_connection, init_db, list_case_documents

    init_db(Path(state_dir))
    conn = get_connection(Path(state_dir))
    try:
        events = get_case_events(conn, case_id)
        docs = list_case_documents(conn, case_id)
    finally:
        conn.close()
    # правило «тексты только из реестра»: покрытие прогона честное — приложенные документы дела
    # сканируются наравне с событиями; нечитаемые (бинарь/нет файла) — явно
    # в docs_skipped, не молча.
    bodies = [(f"event:{e['event_id']}", normalize(e.get("doc_text") or "")) for e in events]
    skipped = 0
    for d in docs:
        try:
            raw = Path(d["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        bodies.append((f"doc:{d['id']}@{d['sha256'][:12]}", normalize(raw)))
    scope = {"events_scanned": len(events), "docs_scanned": len(docs) - skipped,
             "docs_skipped_unreadable": skipped}
    best = {"verdict": "not_found", "location": None, "offset": None,
            "matched_fragment": None, "ratio": None, "threshold": None,
            "snippet": None,
            "detail": (f"case {case_id}: " + ", ".join(f"{k}={v}" for k, v in scope.items())
                       + (" (нечитаемые документы НЕ проверялись — сверь формат)" if skipped else "")),
            **scope}
    for location, body in bodies:
        verdict = _match(query, body, max_fuzzy, location, dict(scope))
        if verdict["verdict"] == "exact":
            return verdict
        if verdict["verdict"] == "fuzzy" and best["verdict"] != "fuzzy":
            best = verdict
    return best


def _source_file(path, query: str, max_fuzzy: float):
    p = Path(path)
    raw = p.read_text(encoding="utf-8")  # OSError propagates to the CLI layer
    body = normalize(raw)
    location = f"file:{p}"
    found = _match(query, body, max_fuzzy, location)
    if found["offset"] is not None:
        found["location"] = f"{location}:{found['offset']}"
    return found


# ---------------------------------------------------------------- entry


def verify_quote(text, source, *, act=None, article=None, date=None,
                 case_id=None, path=None, state_dir=None, max_fuzzy=0.0):
    """Verify ``text`` against one local source. Raises VerifyInputError on
    bad arguments; never touches the network."""
    if not isinstance(text, str) or not text.strip():
        raise VerifyInputError("text must be a non-empty string")
    try:
        max_fuzzy = float(max_fuzzy or 0.0)
    except (TypeError, ValueError):
        raise VerifyInputError("max-fuzzy must be a number")
    if not 0.0 <= max_fuzzy <= 1.0:
        raise VerifyInputError("max-fuzzy must be within 0..1")
    query = normalize(text)
    if not query:
        raise VerifyInputError("text normalizes to empty string")

    if source == "npa":
        if not (act and article and date):
            raise VerifyInputError("source npa requires --act, --article and --date")
        if not DATE_RE.match(date):
            raise VerifyInputError("date must be YYYY-MM-DD")
        payload = _source_npa(act, article, date, query, max_fuzzy)
    elif source == "case":
        if not case_id:
            raise VerifyInputError("source case requires --case-id")
        if state_dir is None:
            raise VerifyInputError("source case requires a state dir")
        payload = _source_case(case_id, state_dir, query, max_fuzzy)
    elif source == "file":
        if not path:
            raise VerifyInputError("source file requires --path")
        if not Path(path).is_file():
            raise VerifyInputError(f"file not found: {path}")
        payload = _source_file(path, query, max_fuzzy)
    else:
        raise VerifyInputError(f"unknown source: {source!r}")

    return {"source": source, "status": payload["verdict"],
            "requested_text": text, "normalized_text": query,
            "max_fuzzy": max_fuzzy, **payload}
