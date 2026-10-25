#!/usr/bin/env python3
"""Recompute MANIFEST.json for a project. The manifest excludes itself."""
import argparse
import datetime
import errno
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile

SKIP_DIRS = {
    # VCS
    ".git", ".hg", ".svn",
    # Python cache / tooling
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox",
    # Virtual environments
    ".venv", "venv",
    # Generic build / packaging
    "build", "dist", "node_modules",
    # Explicitly named runtime-state directories
    ".runtime-state", ".case-search",
}
SKIP_NAMES = {".DS_Store", ".coverage"}


def _temp_prefix(manifest: pathlib.Path) -> str:
    return f".{manifest.name}.tmp-"


def is_stale_temp_file(path: pathlib.Path, manifest: pathlib.Path) -> bool:
    return path.parent == manifest.parent and path.name.startswith(_temp_prefix(manifest))


def validate_timestamp(ts: str | None) -> str:
    if ts is None:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO-8601 timestamp") from exc
    if dt.tzinfo != datetime.timezone.utc:
        raise ValueError("timestamp must be UTC")
    return ts


def _validate_manifest_entries(files, context: str) -> dict[str, dict]:
    if not isinstance(files, list):
        raise ValueError(f"{context} files list is missing or invalid")
    entries = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError(f"{context} entry is not an object")
        path = entry.get("path")
        sha256 = entry.get("sha256")
        bytes_ = entry.get("bytes")
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise ValueError(f"{context} entry missing path or sha256")
        p = pathlib.Path(path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"{context} path is not relative")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
            raise ValueError(f"{context} sha256 is invalid")
        if not isinstance(bytes_, int) or isinstance(bytes_, bool) or bytes_ < 0:
            raise ValueError(f"{context} bytes is invalid")
        if path in entries:
            raise ValueError(f"{context} contains duplicate path")
        entries[path] = entry
    return entries


def load_old_manifest(manifest: pathlib.Path) -> dict[str, dict]:
    fd = _open_nofollow(manifest)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        try:
            raw = json.loads(fh.read())
        except json.JSONDecodeError as exc:
            raise ValueError("existing manifest is malformed") from exc
    if not isinstance(raw, dict):
        raise ValueError("existing manifest is malformed")
    return _validate_manifest_entries(raw.get("files"), "existing manifest")


def _open_nofollow(path: pathlib.Path) -> int:
    flags = os.O_RDONLY
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("platform does not support no-follow opens")
    flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _openat_nofollow(dirfd: int, name: str) -> int:
    flags = os.O_RDONLY
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("platform does not support no-follow opens")
    flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=dirfd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _is_symlink_at(dirfd: int, name: str) -> bool:
    try:
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("scan failed") from exc
    return stat.S_ISLNK(st.st_mode)


def _digest_and_size_at(dirfd: int, name: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    fd = _openat_nofollow(dirfd, name)
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def digest_and_size(path: pathlib.Path) -> tuple[str, int]:
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        return _digest_and_size_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)


def sha256_of(path: pathlib.Path) -> str:
    return digest_and_size(path)[0]


def verify_manifest(manifest: pathlib.Path, records: list[dict]) -> tuple[int, int]:
    fd = _open_nofollow(manifest)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        try:
            data = json.loads(fh.read())
        except json.JSONDecodeError as exc:
            raise ValueError("manifest is malformed") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest is malformed")
    check = _validate_manifest_entries(data.get("files"), "manifest")
    records_by_path = {record["path"]: record for record in records}
    all_paths = set(check) | set(records_by_path)
    match = 0
    for path in all_paths:
        entry = check.get(path)
        record = records_by_path.get(path)
        if entry is None or record is None:
            continue
        if entry.get("bytes") != record.get("bytes") or entry.get("sha256") != record.get("sha256"):
            continue
        match += 1
    return match, len(all_paths)


def write_manifest_atomic(manifest: pathlib.Path, payload: dict) -> None:
    if manifest.is_symlink():
        raise RuntimeError("manifest is a symlink")
    mode = None
    try:
        st = os.lstat(manifest)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError("manifest is a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("manifest is not a regular file")
        mode = stat.S_IMODE(st.st_mode)
    fd, tmp_path = tempfile.mkstemp(
        dir=manifest.parent,
        prefix=_temp_prefix(manifest),
        suffix="",
    )
    tmp = pathlib.Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            fh.flush()
            os.fsync(fh.fileno())
        if manifest.is_symlink():
            raise RuntimeError("manifest is a symlink")
        os.replace(tmp, manifest)
        dir_fd = os.open(manifest.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, TypeError):
            raise ValueError("validation error") from exc
        raise


def collect(root: pathlib.Path, manifest: pathlib.Path) -> list[dict]:
    if not root.is_dir():
        raise RuntimeError("root is not a directory")
    out = []
    scan_errors = []

    def onerror(exc: OSError) -> None:
        scan_errors.append(exc)

    for dirpath, dirnames, filenames, dirfd in os.fwalk(
        root, follow_symlinks=False, onerror=onerror
    ):
        current = pathlib.Path(dirpath)
        rel_parts = current.relative_to(root).parts
        # Inspect skipped directories too; symlinks there must still fail closed.
        for name in dirnames + filenames:
            if _is_symlink_at(dirfd, name):
                raise RuntimeError("symlink found under project root")
        # Exclude ordinary files located beneath skipped directories from the manifest.
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        for name in filenames:
            p = current / name
            if is_stale_temp_file(p, manifest):
                continue
            if name in SKIP_NAMES:
                continue
            if current == root and name == manifest.name:
                continue
            digest, size = _digest_and_size_at(dirfd, name)
            rel = p.relative_to(root).as_posix()
            out.append({"path": rel, "bytes": size, "sha256": digest})

    if scan_errors:
        raise RuntimeError("scan failed")
    return sorted(out, key=lambda e: e["path"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute MANIFEST.json")
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1],
        help="project root directory (default: parent of tools/)",
    )
    parser.add_argument(
        "--timestamp",
        help="ISO 8601 timestamp (default: current UTC)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = root / "MANIFEST.json"

    try:
        records = collect(root, manifest)
        generated_at = validate_timestamp(args.timestamp)
        entries = []
        updated, added = [], []
        old = {}
        if manifest.exists():
            old = load_old_manifest(manifest)

        for record in records:
            rel = record["path"]
            entries.append(record)
            if rel not in old:
                added.append(rel)
            elif old[rel]["sha256"] != record["sha256"]:
                updated.append(rel)

        if manifest.exists() and not entries and old:
            raise RuntimeError("empty scan would erase existing manifest")

        payload = {
            "project": "sud-cli",
            "generated_by": "tools/recalc_manifest.py",
            "generated_at": generated_at,
            "note": "MANIFEST.json исключён — файл не может содержать собственный хэш.",
            "files": entries,
        }
        write_manifest_atomic(manifest, payload)

        print("updated:", len(updated))
        print("added:", len(added))
        print("total entries:", len(entries), "(manifest excluded)")

        match, total = verify_manifest(manifest, records)
        print()
        print(f"verification: {match}/{total} match")
        return 0 if match == total else 1
    except OSError as exc:
        msg = os.strerror(exc.errno) if exc.errno is not None else "filesystem error"
        print(f"error: {msg}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
