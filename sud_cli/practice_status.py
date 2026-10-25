"""Practice/act status probe (S-54, v2.9.0) — локальные артефакты, ничего из воздуха.

``practice status <case-number>`` answers exactly one honest question: is there
a LOCAL record that suggests the case decision was overturned or modified? The
fact base is limited to:

  * saved case-search snapshot files (``--store`` dir, JSON case snapshots
    and ``case-diff`` artifacts — the structures written by
    ``sud_cli.case_diff.diff_case_snapshots`` and the snapshot schema), and
  * an explicit ``--related "<title;title;...>"`` fixture input (offline agent
    hand-off, e.g. titles already collected by case_history in a live session).

The snapshot diff structure carries no overturning field (verified against
``case_search/diff.py``: only path/before/after scalars), so the marker rule
is header-based: a title from instance>1-style records matching the
отмен/измен root marks ``possibly_overturned`` — unless negated ("без
изменения", "оставлено без ..."). Every other answer is ``unknown`` with a
mandatory hint. This module NEVER asserts «действует»/«отменён»: absence of a
local overturn record is not evidence of validity.

Strictly offline: no network primitives are imported here (gate-tested).
"""

import json
import re

from sud_cli import _wm
from sud_cli import __version__

HINT = _wm.render_line("status_hint", __version__)

# Overturn/modification roots; the negation guard keeps affirmations
# ("оставлено без изменения") out of the overturn verdict.
_OVERTURN_RE = re.compile(r"отмен|измен|направлен|новое решение", re.IGNORECASE)
_NEGATED_RE = re.compile(r"без\s+(?:изменени|отмен)|остав[ле]+но\s+без", re.IGNORECASE)


def has_overturn_marker(title: str) -> bool:
    if not isinstance(title, str) or not title.strip():
        return False
    return bool(_OVERTURN_RE.search(title)) and not _NEGATED_RE.search(title)


def _iter_strings(payload):
    """Flat scan of header-like strings from a snapshot/diff JSON payload."""
    out = []
    if not isinstance(payload, dict):
        return out
    for key in ("case_number", "status", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            out.append(value)
    last_event = payload.get("last_event")
    if isinstance(last_event, dict):
        for key in ("event_type", "name", "document_url"):
            value = last_event.get(key)
            if isinstance(value, str) and value:
                out.append(value)
    for event in payload.get("events_timeline") or []:
        if isinstance(event, dict) and isinstance(event.get("name"), str):
            out.append(event["name"])
    for change in payload.get("changes") or []:
        if isinstance(change, dict):
            for key in ("before", "after"):
                value = change.get(key)
                if isinstance(value, str) and value:
                    out.append(value)
    for change in payload.get("timeline_changes") or []:
        if isinstance(change, dict) and isinstance(change.get("name"), str):
            out.append(change["name"])
    return out


def _mentions_case(payload, case_number: str, path) -> bool:
    numbers = [str(payload.get("case_number") or ""), path.stem]
    return any(case_number in num or num in case_number for num in numbers if num)


def scan_store_dir(store_dir, case_number: str):
    """Evidence records {file, title} from local snapshot/diff artifacts."""
    evidence = []
    root = store_dir
    if not root or not root.is_dir():
        return evidence
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("snapshot"), dict):
            payload = payload["snapshot"]
        if not isinstance(payload, dict) or not _mentions_case(payload, case_number, path):
            continue
        for title in _iter_strings(payload):
            if has_overturn_marker(title):
                evidence.append({"file": str(path), "title": title})
    return evidence


def related_records(related):
    """Titles from the explicit --related fixture input (list or ';'-string)."""
    if not related:
        return []
    if isinstance(related, str):
        entries = [item.strip() for item in related.split(";")]
    else:
        entries = [str(item).strip() for item in related]
    return [{"file": f"cli:--related#{i + 1}", "title": entry}
            for i, entry in enumerate(entries) if entry]


def assess_status(case_number, *, related=None, store_dir=None):
    """Pure verdict builder. No network, no state-dir mutation.

    verdict ∈ {possibly_overturned, unknown}; «действует» is not in the
    vocabulary of this command by construction.
    """
    if not isinstance(case_number, str) or not case_number.strip():
        raise ValueError("case_number must be a non-empty string")
    case_number = case_number.strip()

    from pathlib import Path  # local: keep module surface file-system-only

    evidence = scan_store_dir(Path(store_dir) if store_dir else None, case_number)
    evidence.extend(rec for rec in related_records(related) if has_overturn_marker(rec["title"]))

    verdict = "possibly_overturned" if evidence else "unknown"
    return {
        "case_number": case_number,
        "status": verdict,
        "verdict": verdict,
        "evidence": evidence,
        "hint": HINT,
        "coverage": "local snapshot/diff artifacts + --related fixture only; "
                    "no live sudact/case_history call",
    }
