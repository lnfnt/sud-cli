"""Alert artifacts: local JSON files only.

Telegram/cron integration is explicitly NOT implemented (out of scope for the
synthetic phase). An alert names the physical artifact it refers to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .snapshots import WatchResult
from .storage import atomic_write, safe_slug

LOCAL_ONLY = "local-file"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Alert:
    case_number: str
    status: str
    reason: str
    changes: list[dict]
    destination: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "case_number": self.case_number,
            "status": self.status,
            "reason": self.reason,
            "changes": self.changes,
            "destination": self.destination,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2)


def build_alert(case_number: str, result: WatchResult,
                now: str | None = None) -> Alert | None:
    """Return an alert when something observable happened, else None."""
    outcome = result.outcome
    created_at = now or _utc_now_iso()
    if outcome.status != "ok":
        return Alert(case_number, outcome.status, outcome.reason, [],
                     LOCAL_ONLY, created_at)
    if result.changed:
        return Alert(case_number, "changed",
                     "snapshot diff detected",
                     [c.to_dict() for c in result.changes], LOCAL_ONLY,
                     created_at)
    return None


def write_alert(alert: Alert, alerts_dir: Path | str) -> Path:
    directory = Path(alerts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = safe_slug(f"{alert.case_number}@{alert.created_at}-{alert.status}")
    path = directory / f"{name}.json"
    atomic_write(path, alert.to_json() + "\n")
    return path
