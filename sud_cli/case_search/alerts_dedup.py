"""Alert deduplication across watch runs.

A fingerprint is a stable, truncated SHA-256 digest of the logical alert
identity: case number, status, reason, and the sorted set of changes. The
created-at timestamp is intentionally excluded so repeated runs of the same
watch produce the same fingerprint.

The seen-fingerprints store is fail-open: a missing or corrupt store is treated
as empty, so alerts are never silently suppressed because the store broke.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from ._logging import get_logger
from .alerts import Alert
from .snapshots import WatchResult
from .storage import atomic_write, default_storage_root

_log = get_logger("alerts_dedup")

DEFAULT_SEEN_PATH = (
    default_storage_root() / "alerts" / "seen_fingerprints.json"
)


def default_seen_path() -> Path:
    """Runtime-resolved default path for the seen-fingerprints store."""
    return default_storage_root() / "alerts" / "seen_fingerprints.json"


def _canonical_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a deterministic ordering of change dicts."""

    def _sort_key(change: dict[str, Any]) -> tuple[str, str, str]:
        path = change.get("path", "")
        before = json.dumps(change.get("before"), sort_keys=True,
                            ensure_ascii=False, separators=(",", ":"))
        after = json.dumps(change.get("after"), sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
        return (path, before, after)

    return sorted(changes, key=_sort_key)


def _identity_payload(
    case_number: str,
    status: str,
    reason: str,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "case_number": case_number,
        "status": status,
        "reason": reason,
        "changes": _canonical_changes(changes),
    }


def alert_fingerprint(case_number: str, result_or_changes) -> str:
    """Return a stable 16-character hex fingerprint for a logical alert.

    ``result_or_changes`` may be a ``WatchResult``, an ``Alert``, or a list of
    change dicts. The timestamp is never part of the identity.
    """
    if isinstance(result_or_changes, WatchResult):
        status = result_or_changes.outcome.status
        reason = result_or_changes.outcome.reason
        changes = [c.to_dict() for c in result_or_changes.changes]
    elif isinstance(result_or_changes, Alert):
        status = result_or_changes.status
        reason = result_or_changes.reason
        changes = result_or_changes.changes
    elif isinstance(result_or_changes, list):
        # Bare list of changes: assume the standard "changed" alert shape.
        status = "changed"
        reason = "snapshot diff detected"
        changes = list(result_or_changes)
    else:
        raise TypeError(
            f"expected WatchResult, Alert, or list of changes, got "
            f"{type(result_or_changes).__name__}"
        )

    payload = _identity_payload(case_number, status, reason, changes)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_seen_fingerprints(store_path: Path) -> dict[str, str]:
    """Load the map of fingerprint -> ISO timestamp from disk.

    Missing or corrupt files are treated as empty, with a warning logged.
    """
    if not store_path.is_file():
        return {}

    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning(
            "corrupt seen-fingerprints store %s (%s); treating as empty",
            store_path, exc,
        )
        return {}

    if not isinstance(data, dict):
        _log.warning(
            "corrupt seen-fingerprints store %s (not a JSON object); "
            "treating as empty", store_path,
        )
        return {}

    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def record_fingerprints(store_path: Path, fps: dict[str, str]) -> None:
    """Persist the fingerprint map atomically (temp file + os.replace)."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(fps, sort_keys=True, ensure_ascii=False, indent=2)
    atomic_write(store_path, payload + "\n")


def is_duplicate(store_path: Path, fp: str) -> bool:
    """Return True if ``fp`` already exists in the store."""
    return fp in load_seen_fingerprints(store_path)


def filter_seen(
    store_path: Path,
    alerts: Iterable[Alert],
) -> tuple[list[Alert], int]:
    """Split ``alerts`` into (new_alerts, duplicate_count).

    The store is read once and is not modified.
    """
    seen = load_seen_fingerprints(store_path)
    new_alerts: list[Alert] = []
    dup_count = 0
    for alert in alerts:
        fp = alert_fingerprint(alert.case_number, alert)
        if fp in seen:
            dup_count += 1
        else:
            new_alerts.append(alert)
    return new_alerts, dup_count
