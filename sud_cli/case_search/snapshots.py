"""Snapshot bookkeeping: compare a fresh fetch against the stored baseline.

On degraded/error outcomes the previous snapshot is preserved untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diff import Change, diff_snapshots
from .pipeline import FetchOutcome
from .schema import CaseSnapshot
from .storage import SnapshotStore, StoredSnapshot


@dataclass(frozen=True)
class WatchResult:
    outcome: FetchOutcome
    changes: tuple[Change, ...]
    stored: StoredSnapshot | None
    baseline_preserved: bool

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def watch_case(store: SnapshotStore, outcome: FetchOutcome) -> WatchResult:
    if outcome.snapshot is None:
        return WatchResult(outcome, (), None, baseline_preserved=True)

    old = store.load_latest(outcome.snapshot.case_number)
    changes = diff_snapshots(old, outcome.snapshot) if old else []
    stored = store.save(outcome.snapshot)
    return WatchResult(outcome, tuple(changes), stored,
                       baseline_preserved=False)


def summary_line(snapshot: CaseSnapshot) -> str:
    hearing = snapshot.next_hearing
    hearing_txt = f"{hearing.date} {hearing.time}" if hearing else "none"
    return (f"{snapshot.case_number} | {snapshot.status} | "
            f"next hearing: {hearing_txt} | judge: {snapshot.judge} | "
            f"events: {len(snapshot.events_timeline)}")
