"""Build a version diff over two saved snapshots — mirrors case-search semantics.

case-search has ``diff_snapshots`` in ``case_search/diff.py:59`` but never
exposes it: no CLI subcommand calls it and ``public_api.py`` does not re-export
it (verified by grep over the whole package and by ``case-search --help``).
sud-cli therefore implements the same field set on its own, reading the two
snapshot JSON files that the import commands already write into the state
directory. case-search itself is untouched.

The compared fields are copied from ``case_search.diff._SCALARS`` so the two
implementations agree on what counts as a change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Copied from case_search/diff.py::_SCALARS.
SCALAR_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("status", ("status",)),
    ("court", ("court",)),
    ("judge", ("judge",)),
    ("updated_at", ("updated_at",)),
    ("next_hearing.date", ("next_hearing", "date")),
    ("next_hearing.time", ("next_hearing", "time")),
    ("next_hearing.hall", ("next_hearing", "hall")),
    ("next_hearing.type", ("next_hearing", "type")),
    ("last_event.date", ("last_event", "date")),
    ("last_event.event_type", ("last_event", "event_type")),
    ("last_event.document_url", ("last_event", "document_url")),
)


class DiffError(Exception):
    """A snapshot file is missing, unreadable or not a case snapshot."""


def _dig(payload: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _load(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiffError(f"cannot read snapshot: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiffError(f"snapshot is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DiffError("snapshot must be a JSON object")
    return payload


def _snapshot_payload(path: Path) -> Dict[str, Any]:
    """Return the snapshot object, unwrapping an ``auto-import`` envelope.

    ``auto-import`` wraps its result as ``{"snapshot": {...}, "extensions": ...}``
    while ``sudrf-import`` writes the snapshot at the top level. Accepting both
    keeps the command usable regardless of which importer produced the file.
    """
    payload = _load(path)
    inner = payload.get("snapshot")
    if isinstance(inner, dict):
        return inner
    return payload


def _parties(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    parties = payload.get("parties")
    if not isinstance(parties, dict):
        return {"plaintiff": [], "respondent": [], "third_parties": []}
    return {
        key: list(parties.get(key) or [])
        for key in ("plaintiff", "respondent", "third_parties")
    }


def _timeline_index(payload: Dict[str, Any]) -> Dict[Tuple[str, str], bool]:
    index: Dict[Tuple[str, str], bool] = {}
    for event in payload.get("events_timeline") or []:
        if not isinstance(event, dict):
            continue
        date = event.get("date")
        name = event.get("name")
        if date is None or name is None:
            continue
        index[(str(date), str(name))] = bool(event.get("published"))
    return index


def diff_case_snapshots(
    old_path: Path, new_path: Path
) -> Dict[str, Any]:
    """Compare two saved snapshots of the same case.

    Returns a dict with ``changed``, ``changes`` (path/before/after) and
    ``added_removed_events``. An unchanged pair yields ``changed: False`` with
    empty lists rather than an error, because "nothing moved" is a legitimate
    answer to the question.
    """
    old = _snapshot_payload(old_path)
    new = _snapshot_payload(new_path)

    old_number = str(old.get("case_number") or "")
    new_number = str(new.get("case_number") or "")
    if not old_number or not new_number:
        raise DiffError("snapshot has no case_number")

    # Check identity *before* comparing: a field-by-field report across two
    # different cases would be noise dressed up as a diff.
    if old_number != new_number:
        raise DiffError(
            f"snapshots belong to different cases: {old_number} vs {new_number}"
        )

    changes: List[Dict[str, Any]] = []
    for path, keys in SCALAR_FIELDS:
        before = _dig(old, keys)
        after = _dig(new, keys)
        if before != after:
            changes.append({"path": path, "before": before, "after": after})

    before_parties = _parties(old)
    after_parties = _parties(new)
    for role in ("plaintiff", "respondent", "third_parties"):
        if before_parties[role] != after_parties[role]:
            changes.append({
                "path": f"parties.{role}",
                "before": before_parties[role],
                "after": after_parties[role],
            })

    old_tl = _timeline_index(old)
    new_tl = _timeline_index(new)
    timeline_changes: List[Dict[str, Any]] = []
    for key in sorted(set(old_tl) | set(new_tl)):
        was, now = old_tl.get(key), new_tl.get(key)
        if was != now:
            timeline_changes.append({
                "date": key[0],
                "name": key[1],
                "before": was,
                "after": now,
            })

    return {
        "case_number": new_number,
        "changed": bool(changes or timeline_changes),
        "changes": sorted(changes, key=lambda c: c["path"]),
        "timeline_changes": timeline_changes,
        "compared_fields": len(SCALAR_FIELDS) + 3,
        "source": {
            "old": str(old_path),
            "new": str(new_path),
        },
    }