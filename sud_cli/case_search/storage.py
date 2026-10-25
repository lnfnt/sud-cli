"""Local snapshot storage. Snapshot files are the only persisted artifacts.

Layout: <root>/<case-slug>/<content-hash>.json plus a `latest.json` pointer.
Nothing here leaves the machine; nothing here is a real case.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .errors import StorageError
from .schema import CaseSnapshot


def default_storage_root() -> Path:
    """Default on-disk root used by ``SnapshotStore``.

    Resolution: configured ``store_dir`` > ``.case-search`` in cwd.
    Kept in one place so ``doctor`` and the CLI agree on the path.
    """
    from . import config as _cfg

    configured = _cfg.get("store_dir")
    return Path(configured) if configured else Path(".case-search")


def slugify(case_number: str) -> str:
    return sha256(case_number.encode("utf-8")).hexdigest()[:16]


def safe_slug(case_number: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", case_number).strip("-")
    return f"{cleaned}-{slugify(case_number)}" if cleaned else slugify(
        case_number)


def atomic_write(path: Path, text: str) -> None:
    """Write a file atomically so a crash cannot leave a truncated artifact."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class StoredSnapshot:
    path: Path
    content_hash: str


class SnapshotStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def save(self, snapshot: CaseSnapshot) -> StoredSnapshot:
        case_dir = self.root / safe_slug(snapshot.case_number)
        case_dir.mkdir(parents=True, exist_ok=True)
        content_hash = snapshot.content_hash()
        path = case_dir / f"{content_hash}.json"
        atomic_write(path, snapshot.to_json() + "\n")
        atomic_write(
            case_dir / "latest.json",
            json.dumps({"content_hash": content_hash,
                        "file": path.name}, sort_keys=True) + "\n")
        return StoredSnapshot(path, content_hash)

    def load_latest(self, case_number: str) -> CaseSnapshot | None:
        case_dir = self.root / safe_slug(case_number)
        pointer = case_dir / "latest.json"
        if not pointer.is_file():
            return None
        try:
            meta = json.loads(pointer.read_text(encoding="utf-8"))
            filename = meta["file"]
            content_hash = meta["content_hash"]
        except (json.JSONDecodeError, KeyError, TypeError,
                UnicodeDecodeError, AttributeError) as exc:
            raise StorageError(
                f"corrupt snapshot pointer: {pointer}") from exc
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise StorageError(f"unsafe snapshot pointer entry: {filename!r}")
        if not isinstance(content_hash, str) or not content_hash.strip():
            raise StorageError("snapshot pointer has no content hash")
        path = case_dir / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StorageError(f"unreadable snapshot file: {path}") from exc
        try:
            snapshot = snapshot_from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"invalid snapshot structure: {path}") from exc
        if snapshot.content_hash() != content_hash:
            raise StorageError(f"snapshot hash mismatch: {path}")
        return snapshot


def snapshot_from_dict(data: dict) -> CaseSnapshot:
    """Rehydrate a snapshot from its canonical dict form."""
    from .schema import (Hearing, LastEvent, Parties, Provenance,
                         TimelineEvent)
    try:
        prov = data["provenance"]
        nh = data.get("next_hearing")
        le = data.get("last_event")
        parties = data["parties"]
        return CaseSnapshot(
            case_number=data["case_number"],
            court=data["court"],
            judge=data["judge"],
            status=data["status"],
            parties=Parties(
                plaintiff=tuple(parties["plaintiff"]),
                respondent=tuple(parties["respondent"]),
                third_parties=tuple(parties["third_parties"]),
            ),
            next_hearing=Hearing(**nh) if nh else None,
            last_event=LastEvent(**le) if le else None,
            events_timeline=tuple(
                TimelineEvent(**e) for e in data["events_timeline"]),
            updated_at=data["updated_at"],
            provenance=Provenance(
                source=prov["source"],
                source_url=prov["source_url"],
                retrieved_at=prov["retrieved_at"],
                parser_version=prov["parser_version"],
                raw_artifact_hash=prov["raw_artifact_hash"],
                warnings=tuple(prov["warnings"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError(f"invalid snapshot structure: {exc}") from exc
