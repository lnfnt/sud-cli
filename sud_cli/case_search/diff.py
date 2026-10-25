"""Diff engine between two canonical snapshots.

Produces a sorted list of atomic changes so output is deterministic regardless
of dict insertion order. Events are keyed by (date, name) and compared by
publication state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import CaseSnapshot

_SCALARS = (
    ("status", "status"),
    ("court", "court"),
    ("judge", "judge"),
    ("updated_at", "updated_at"),
    ("next_hearing.date", ("next_hearing", "date")),
    ("next_hearing.time", ("next_hearing", "time")),
    ("next_hearing.hall", ("next_hearing", "hall")),
    ("next_hearing.type", ("next_hearing", "type")),
    ("last_event.date", ("last_event", "date")),
    ("last_event.event_type", ("last_event", "event_type")),
    ("last_event.document_url", ("last_event", "document_url")),
)


@dataclass(frozen=True)
class Change:
    path: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "before": self.before,
                "after": self.after}


def _scalar(snapshot: CaseSnapshot, key: Any) -> Any:
    if isinstance(key, str):
        return getattr(snapshot, key)
    obj: Any = getattr(snapshot, key[0])
    if obj is None:
        return None
    return getattr(obj, key[1])


def _timeline_key(event: dict[str, Any]) -> tuple[str, str]:
    return (event["date"], event["name"])


def _timeline_index(snapshot: CaseSnapshot) -> dict[tuple, bool]:
    return {_timeline_key(e.to_dict()): e.published
            for e in snapshot.events_timeline}


def diff_snapshots(old: CaseSnapshot, new: CaseSnapshot) -> list[Change]:
    changes: list[Change] = []

    for path, key in _SCALARS:
        before, after = _scalar(old, key), _scalar(new, key)
        if before != after:
            changes.append(Change(path, before, after))

    before_parties = old.parties.to_dict()
    after_parties = new.parties.to_dict()
    for role in ("plaintiff", "respondent", "third_parties"):
        if before_parties[role] != after_parties[role]:
            changes.append(Change(f"parties.{role}", before_parties[role],
                                  after_parties[role]))

    old_tl, new_tl = _timeline_index(old), _timeline_index(new)
    for key in sorted(set(old_tl) | set(new_tl)):
        date, name = key
        path = f"event[{date}|{name}].published"
        if key not in old_tl:
            changes.append(Change(path, None, new_tl[key]))
        elif key not in new_tl:
            changes.append(Change(path, old_tl[key], None))
        elif old_tl[key] != new_tl[key]:
            changes.append(Change(path, old_tl[key], new_tl[key]))

    return sorted(changes, key=lambda c: c.path)


def has_changes(changes: list[Change]) -> bool:
    return bool(changes)
