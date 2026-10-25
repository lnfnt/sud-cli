"""Synthetic tests for tools/recalc_manifest.py"""
import datetime
import errno
import hashlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tools.recalc_manifest as recalc


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "recalc_manifest.py"


class TestRecalcManifest(unittest.TestCase):
    def run_script(self, root: pathlib.Path, *extra_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), *extra_args],
            capture_output=True,
            text=True,
        )

    def test_accepts_root_argument_and_creates_manifest(self):
        """--root should be accepted and MANIFEST.json created under that root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("hello", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = root / "MANIFEST.json"
            self.assertTrue(manifest.exists())
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual([e["path"] for e in data["files"]], ["file.txt"])

    def test_excludes_build_cache_vcs_and_runtime_dirs(self):
        """Generic build/cache/VCS and the synthetic runtime-state directory are excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src" / "keep.py").parent.mkdir(parents=True)
            (root / "src" / "keep.py").write_text("x", encoding="utf-8")
            (root / "db" / "schema.sql").parent.mkdir(parents=True)
            (root / "db" / "schema.sql").write_text("z", encoding="utf-8")
            for name in (".git", "__pycache__", ".pytest_cache", ".ruff_cache",
                         ".venv", "venv", "node_modules", "build", "dist",
                         ".tox", ".mypy_cache", ".runtime-state"):
                (root / name / "noise.txt").parent.mkdir(exist_ok=True)
                (root / name / "noise.txt").write_text("y", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
            paths = {e["path"] for e in data["files"]}
            self.assertEqual(paths, {"src/keep.py", "db/schema.sql"})

    def test_skip_dirs_uses_parent_parts_only(self):
        """SKIP_DIRS inspects only directory parts; file-only exclusions live in SKIP_NAMES."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "build").write_text("i am a file", encoding="utf-8")
            (root / ".coverage").write_text("coverage data", encoding="utf-8")
            (root / "src" / "build" / "inner.py").parent.mkdir(parents=True)
            (root / "src" / "build" / "inner.py").write_text("x", encoding="utf-8")
            (root / "ordinary.py").write_text("y", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = {e["path"] for e in json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))["files"]}
            self.assertEqual(paths, {"build", "ordinary.py"})

    def test_symlinks_fail_closed(self):
        """Any symlink under root makes the run abort without following it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "real.txt").write_text("data", encoding="utf-8")
            (root / "link.txt").symlink_to(root / "real.txt")
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr.lower())
            self.assertNotIn("link.txt", result.stderr)
            self.assertFalse((root / "MANIFEST.json").exists())

    def test_dirfd_bounds_race_with_symlink_swap(self):
        """A directory swapped for a symlink after traversal still hashes the original entry."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "root"
            root.mkdir()
            outside = pathlib.Path(tmp) / "outside"
            outside.mkdir()
            (outside / "evil.txt").write_text("evil", encoding="utf-8")

            target_dir = root / "target"
            target_dir.mkdir()
            (target_dir / "good.txt").write_text("good", encoding="utf-8")

            good_hash = hashlib.sha256(b"good").hexdigest()
            original_fwalk = os.fwalk

            def racing_fwalk(path, *args, **kwargs):
                for dirpath, dirnames, filenames, dirfd in original_fwalk(path, *args, **kwargs):
                    if pathlib.Path(dirpath) == target_dir and "good.txt" in filenames:
                        renamed = root / "target_renamed"
                        target_dir.rename(renamed)
                        target_dir.symlink_to(outside)
                    yield (dirpath, dirnames, filenames, dirfd)

            stderr_capture = io.StringIO()
            with mock.patch("tools.recalc_manifest.os.fwalk", side_effect=racing_fwalk):
                with mock.patch("sys.argv", ["recalc_manifest.py", "--root", str(root)]):
                    with mock.patch("sys.stderr", stderr_capture):
                        rc = recalc.main()

            manifest = root / "MANIFEST.json"
            self.assertEqual(rc, 0, stderr_capture.getvalue())
            self.assertTrue(manifest.exists())
            data = json.loads(manifest.read_text(encoding="utf-8"))
            entries = {e["path"]: e for e in data["files"]}
            self.assertIn("target/good.txt", entries)
            self.assertEqual(entries["target/good.txt"]["sha256"], good_hash)

    def test_manifest_symlink_fails_closed(self):
        """A MANIFEST.json symlink aborts the run and leaves its target unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("data", encoding="utf-8")
            target = root / "real.json"
            target.write_text('{"target": "untouched"}', encoding="utf-8")
            (root / "MANIFEST.json").symlink_to(target)
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(target), result.stderr)
            self.assertNotIn("MANIFEST.json", result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"target": "untouched"}')
            self.assertTrue((root / "MANIFEST.json").is_symlink())

    def test_malformed_old_manifest_fails_cleanly(self):
        """Malformed existing MANIFEST.json fails with a concise error and is unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("x", encoding="utf-8")
            manifest = root / "MANIFEST.json"
            manifest.write_text("not valid json", encoding="utf-8")
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("manifest", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(manifest.read_text(encoding="utf-8"), "not valid json")

    def test_load_old_manifest_rejects_symlink(self):
        """load_old_manifest refuses to follow a symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real.json"
            real.write_text('{"files":[]}', encoding="utf-8")
            link = pathlib.Path(tmp) / "MANIFEST.json"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                recalc.load_old_manifest(link)

    def test_old_manifest_schema_validated(self):
        """Invalid existing manifest schema fails cleanly and leaves the file unchanged."""
        valid_hash = "a" * 64
        bad_cases = [
            ({"generated_at": "x"}, "missing files"),
            ({"files": "not-a-list"}, "non-list files"),
            ({"files": [{}]}, "missing path/sha256"),
            ({"files": [{"path": "x"}]}, "missing sha256"),
            ({"files": [{"sha256": valid_hash}]}, "missing path"),
            ({"files": ["not-an-object"]}, "wrong entry type"),
            ({"files": [{"path": "/absolute", "sha256": valid_hash, "bytes": 0}]}, "absolute path"),
            ({"files": [{"path": "../escape", "sha256": valid_hash, "bytes": 0}]}, "parent reference path"),
            ({"files": [{"path": "x", "sha256": "short", "bytes": 0}]}, "short sha256"),
            ({"files": [{"path": "x", "sha256": "g" * 64, "bytes": 0}]}, "non-hex sha256"),
            ({"files": [{"path": "x", "sha256": valid_hash}]}, "missing bytes"),
            ({"files": [{"path": "x", "sha256": valid_hash, "bytes": -1}]}, "negative bytes"),
            ({"files": [{"path": "x", "sha256": valid_hash, "bytes": "1"}]}, "non-int bytes"),
        ]
        for obj, label in bad_cases:
            content = json.dumps(obj)
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    (root / "file.txt").write_text("x", encoding="utf-8")
                    manifest = root / "MANIFEST.json"
                    manifest.write_text(content, encoding="utf-8")
                    result = self.run_script(root)
                    self.assertNotEqual(result.returncode, 0, label)
                    self.assertIn("manifest", result.stderr.lower(), label)
                    self.assertNotIn("Traceback", result.stderr, label)
                    self.assertEqual(manifest.read_text(encoding="utf-8"), content, label)

    def test_manifest_excludes_itself(self):
        """MANIFEST.json must never appear in its own file list."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("hello", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
            paths = [e["path"] for e in data["files"]]
            self.assertNotIn("MANIFEST.json", paths)
            self.assertEqual(paths, ["file.txt"])

    def test_timestamp_is_current_utc_or_supplied(self):
        """generated_at uses current UTC by default or a supplied timestamp."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("hello", encoding="utf-8")
            before = datetime.datetime.now(datetime.timezone.utc)
            result = self.run_script(root)
            after = datetime.datetime.now(datetime.timezone.utc)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
            generated = datetime.datetime.fromisoformat(data["generated_at"])
            self.assertTrue(before <= generated <= after)

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("hello", encoding="utf-8")
            result = self.run_script(root, "--timestamp", "2025-01-02T03:04:05+00:00")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(data["generated_at"], "2025-01-02T03:04:05+00:00")

    def test_output_shows_aggregate_counts_only(self):
        """Stdout reports counts, never individual file paths."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "old.txt").write_text("v1", encoding="utf-8")
            self.run_script(root)
            (root / "old.txt").write_text("v2", encoding="utf-8")
            (root / "new.txt").write_text("x", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("updated: 1", result.stdout)
            self.assertIn("added: 1", result.stdout)
            self.assertNotIn("old.txt", result.stdout)
            self.assertNotIn("new.txt", result.stdout)

    def test_skip_dirs_only_relative_to_root(self):
        """SKIP_DIRS applies only to directory components inside the root."""
        with tempfile.TemporaryDirectory() as tmp:
            # Project root itself sits under an absolute path component named tmp.
            root = pathlib.Path(tmp) / "tmp" / "project"
            root.mkdir(parents=True)
            (root / "keep.txt").write_text("x", encoding="utf-8")
            (root / "file.tmp").write_text("tempfile", encoding="utf-8")
            (root / "run").write_text("runfile", encoding="utf-8")
            (root / "db" / "source.py").parent.mkdir(parents=True)
            (root / "db" / "source.py").write_text("z", encoding="utf-8")
            (root / ".runtime-state" / "noise.txt").parent.mkdir(parents=True)
            (root / ".runtime-state" / "noise.txt").write_text("y", encoding="utf-8")
            (root / "__pycache__" / "cached.pyc").parent.mkdir(parents=True)
            (root / "__pycache__" / "cached.pyc").write_text("c", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = {e["path"] for e in json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))["files"]}
            self.assertEqual(paths, {"keep.txt", "file.tmp", "run", "db/source.py"})

    def test_invalid_timestamp_rejected_before_change(self):
        """Invalid or non-UTC --timestamp is rejected, no traceback, manifest unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("x", encoding="utf-8")
            manifest = root / "MANIFEST.json"
            manifest.write_text('{"files":[]}', encoding="utf-8")
            result = self.run_script(root, "--timestamp", "not-a-timestamp")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("timestamp", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"files":[]}')

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("x", encoding="utf-8")
            manifest = root / "MANIFEST.json"
            manifest.write_text('{"files":[]}', encoding="utf-8")
            result = self.run_script(root, "--timestamp", "2025-01-02T03:04:05+02:00")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("utc", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"files":[]}')

    def test_manifest_atomic_replace(self):
        """Manifest is atomically replaced; no generator-owned temp file remains."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("hello", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = root / "MANIFEST.json"
            self.assertTrue(manifest.exists())
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual([e["path"] for e in data["files"]], ["file.txt"])
            self.assertEqual(list(root.glob(".MANIFEST.json.tmp-*")), [])

    def test_write_manifest_atomic_replaces_existing_manifest(self):
        """write_manifest_atomic overwrites an existing manifest and leaves no temp file."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text("old content", encoding="utf-8")
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            recalc.write_manifest_atomic(manifest, payload)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(pathlib.Path(tmp).glob(".MANIFEST.json.tmp-*")), [])

    def test_directory_fsync_failure_returns_nonzero_without_path(self):
        """A directory fsync failure after replace returns nonzero without leaking paths."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("x", encoding="utf-8")
            real_fsync = os.fsync

            def failing_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(errno.EIO, "I/O error")
                real_fsync(fd)

            stderr_capture = io.StringIO()
            with mock.patch("tools.recalc_manifest.os.fsync", side_effect=failing_fsync):
                with mock.patch("sys.argv", ["recalc_manifest.py", "--root", str(root)]):
                    with mock.patch("sys.stderr", stderr_capture):
                        rc = recalc.main()
            self.assertNotEqual(rc, 0)
            stderr_text = stderr_capture.getvalue()
            self.assertIn("error:", stderr_text.lower())
            self.assertNotIn("Traceback", stderr_text)
            self.assertNotIn(str(root), stderr_text)
            self.assertNotIn("MANIFEST.json", stderr_text)

    def test_write_manifest_atomic_preserves_existing_mode(self):
        """write_manifest_atomic preserves the permissions of an existing manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text("old content", encoding="utf-8")
            manifest.chmod(0o600)
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            recalc.write_manifest_atomic(manifest, payload)
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), payload)

    def test_write_manifest_atomic_uses_fchmod_not_path_chmod(self):
        """Existing mode is applied to the open fd so a swapped symlink is not chmod-ed by path."""
        if not hasattr(os, "fchmod"):
            self.skipTest("platform lacks os.fchmod")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text("old content", encoding="utf-8")
            manifest.chmod(0o600)
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            with mock.patch("tools.recalc_manifest.os.chmod", side_effect=RuntimeError("path chmod followed symlink")):
                recalc.write_manifest_atomic(manifest, payload)
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), payload)

    def test_write_manifest_atomic_rejects_manifest_symlink(self):
        """write_manifest_atomic rejects a MANIFEST.json symlink before writing."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            real = pathlib.Path(tmp) / "real.json"
            real.write_text("real", encoding="utf-8")
            manifest.symlink_to(real)
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            with self.assertRaises(RuntimeError):
                recalc.write_manifest_atomic(manifest, payload)
            self.assertTrue(manifest.is_symlink())
            self.assertEqual(real.read_text(encoding="utf-8"), "real")
            self.assertEqual(list(pathlib.Path(tmp).glob(".MANIFEST.json.tmp-*")), [])

    def test_write_manifest_atomic_rejects_non_regular_destination(self):
        """write_manifest_atomic rejects a non-regular file destination before reading mode."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.mkdir()
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            with self.assertRaises(RuntimeError):
                recalc.write_manifest_atomic(manifest, payload)
            self.assertTrue(manifest.is_dir())
            self.assertEqual(list(pathlib.Path(tmp).glob(".MANIFEST.json.tmp-*")), [])

    def test_write_manifest_atomic_rechecks_symlink_before_replace(self):
        """write_manifest_atomic re-checks MANIFEST.json is not a symlink before replacing."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            real = pathlib.Path(tmp) / "real.json"
            real.write_text("real", encoding="utf-8")
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            calls = [0]
            original_is_symlink = pathlib.Path.is_symlink

            def toggling_is_symlink(self):
                if self == manifest:
                    calls[0] += 1
                    return calls[0] > 1
                return original_is_symlink(self)

            with mock.patch("tools.recalc_manifest.pathlib.Path.is_symlink", toggling_is_symlink):
                with self.assertRaises(RuntimeError):
                    recalc.write_manifest_atomic(manifest, payload)
            self.assertGreaterEqual(calls[0], 2)
            self.assertEqual(list(pathlib.Path(tmp).glob(".MANIFEST.json.tmp-*")), [])

    def test_write_manifest_atomic_cleans_temp_on_serialization_failure(self):
        """A JSON TypeError is converted to a validation error and leaves no temp file."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            with mock.patch("tools.recalc_manifest.json.dump", side_effect=TypeError("boom")):
                with self.assertRaises(ValueError) as ctx:
                    recalc.write_manifest_atomic(manifest, payload)
                self.assertIn("validation", str(ctx.exception).lower())
            self.assertEqual(list(pathlib.Path(tmp).glob(".MANIFEST.json.tmp-*")), [])

    def test_main_reports_json_typeerror_without_path(self):
        """A JSON TypeError is reported as a concise path-free validation error."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("x", encoding="utf-8")
            stderr_capture = io.StringIO()
            with mock.patch("tools.recalc_manifest.json.dump", side_effect=TypeError("bad type")):
                with mock.patch("sys.argv", ["recalc_manifest.py", "--root", str(root)]):
                    with mock.patch("sys.stderr", stderr_capture):
                        rc = recalc.main()
            self.assertNotEqual(rc, 0)
            stderr_text = stderr_capture.getvalue()
            self.assertIn("error:", stderr_text.lower())
            self.assertIn("validation", stderr_text.lower())
            self.assertNotIn("Traceback", stderr_text)
            self.assertNotIn(str(root), stderr_text)
            self.assertNotIn("file.txt", stderr_text)
            self.assertEqual(list(root.glob(".MANIFEST.json.tmp-*")), [])

    def test_write_manifest_atomic_ignores_preexisting_tmp_symlink(self):
        """A pre-existing MANIFEST.tmp symlink is not followed or removed."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            preexisting = manifest.with_suffix(".tmp")
            preexisting.symlink_to(pathlib.Path(tmp) / "file.txt")
            payload = {"files": [{"path": "file.txt", "sha256": "x", "bytes": 1}]}
            recalc.write_manifest_atomic(manifest, payload)
            self.assertTrue(manifest.exists())
            self.assertTrue(preexisting.is_symlink())
            self.assertEqual(list(pathlib.Path(tmp).glob(".MANIFEST.json.tmp-*")), [])

    def test_stale_temp_skipped_only_in_manifest_parent(self):
        """Only reserved regular stale temp files in the manifest parent are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".MANIFEST.json.tmp-abc123").write_text("stale", encoding="utf-8")
            (root / ".manifest-other.tmp").write_text("unrelated root", encoding="utf-8")
            nested = root / "src" / ".manifest-other.tmp"
            nested.parent.mkdir(parents=True)
            nested.write_text("unrelated nested", encoding="utf-8")
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = {e["path"] for e in json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))["files"]}
            self.assertEqual(paths, {".manifest-other.tmp", "src/.manifest-other.tmp", "keep.txt"})

    def test_stale_temp_symlink_rejected(self):
        """A symlink with the reserved temp name is rejected without leaking the path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            real = root / "real.txt"
            real.write_text("data", encoding="utf-8")
            link = root / ".MANIFEST.json.tmp-abc123"
            link.symlink_to(real)
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr.lower())
            self.assertNotIn("MANIFEST.json", result.stderr)
            self.assertFalse((root / "MANIFEST.json").exists())

    def test_verification_reports_missing_record(self):
        """A record absent from the manifest is counted as a mismatch, no traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({"files": []}), encoding="utf-8")
            records = [{"path": "gone.txt", "bytes": 1, "sha256": "a" * 64}]
            match, total = recalc.verify_manifest(manifest, records)
            self.assertEqual(total, 1)
            self.assertEqual(match, 0)

    def test_verification_reports_changed_hash(self):
        """A manifest entry with a mismatched hash is counted as a mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({
                "files": [{"path": "file.txt", "bytes": 1, "sha256": "a" * 64}]
            }), encoding="utf-8")
            records = [{"path": "file.txt", "bytes": 1, "sha256": "b" * 64}]
            match, total = recalc.verify_manifest(manifest, records)
            self.assertEqual(total, 1)
            self.assertEqual(match, 0)

    def test_verification_compares_bytes_and_sha256(self):
        """verify_manifest checks both recorded bytes and recorded sha256."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({
                "files": [{"path": "file.txt", "bytes": 999, "sha256": "a" * 64}]
            }), encoding="utf-8")
            records = [{"path": "file.txt", "bytes": 1, "sha256": "a" * 64}]
            match, total = recalc.verify_manifest(manifest, records)
            self.assertEqual(total, 1)
            self.assertEqual(match, 0)

    def test_verification_reports_extra_record(self):
        """An unexpected extra manifest entry prevents full agreement."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({
                "files": [
                    {"path": "file.txt", "bytes": 1, "sha256": "a" * 64},
                    {"path": "extra.txt", "bytes": 2, "sha256": "b" * 64},
                ]
            }), encoding="utf-8")
            records = [{"path": "file.txt", "bytes": 1, "sha256": "a" * 64}]
            match, total = recalc.verify_manifest(manifest, records)
            self.assertNotEqual(match, total)
            self.assertEqual(total, 2)

    def test_load_old_manifest_rejects_duplicate_paths(self):
        """Duplicate path entries in an existing manifest are rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({
                "files": [
                    {"path": "file.txt", "bytes": 1, "sha256": "a" * 64},
                    {"path": "file.txt", "bytes": 2, "sha256": "b" * 64},
                ]
            }), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                recalc.load_old_manifest(manifest)
            self.assertIn("duplicate", str(ctx.exception).lower())

    def test_verify_manifest_rejects_duplicate_paths(self):
        """Duplicate path entries in a verification manifest are rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({
                "files": [
                    {"path": "file.txt", "bytes": 1, "sha256": "a" * 64},
                    {"path": "file.txt", "bytes": 2, "sha256": "b" * 64},
                ]
            }), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                recalc.verify_manifest(manifest, [{"path": "file.txt", "bytes": 1, "sha256": "a" * 64}])
            self.assertIn("duplicate", str(ctx.exception).lower())

    def test_digest_and_size_uses_single_stream(self):
        """digest_and_size computes both fields in one streaming pass."""
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "file.txt"
            p.write_text("hello world", encoding="utf-8")
            digest, size = recalc.digest_and_size(p)
            self.assertEqual(size, 11)
            self.assertEqual(digest, recalc.sha256_of(p))

    def test_digest_and_size_rejects_symlink(self):
        """digest_and_size refuses to follow a symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real.txt"
            real.write_text("secret", encoding="utf-8")
            link = pathlib.Path(tmp) / "link.txt"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                recalc.digest_and_size(link)

    def test_open_nofollow_rejects_symlink(self):
        """_open_nofollow raises OSError when path is a symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real.txt"
            real.write_text("secret", encoding="utf-8")
            link = pathlib.Path(tmp) / "link.txt"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                recalc._open_nofollow(link)

    def test_verification_malformed_manifest_fails_cleanly(self):
        """A malformed verification manifest fails cleanly without leaking filenames."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text("not valid json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                recalc.verify_manifest(manifest, [])
            self.assertNotIn(str(manifest), str(ctx.exception))

    def test_verify_manifest_rejects_symlink(self):
        """verify_manifest refuses to follow a symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real.json"
            real.write_text('{"files":[]}', encoding="utf-8")
            link = pathlib.Path(tmp) / "MANIFEST.json"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                recalc.verify_manifest(link, [])

    def test_verification_malformed_entry_fails_cleanly(self):
        """A verification manifest with an invalid entry fails cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "MANIFEST.json"
            manifest.write_text(json.dumps({
                "files": [{"path": "file.txt", "sha256": "short", "bytes": 1}]
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                recalc.verify_manifest(manifest, [{"path": "file.txt", "bytes": 1, "sha256": "a" * 64}])

    def test_main_reports_nonexistent_root_without_path(self):
        """A non-existent --root is reported concisely without leaking the path."""
        with tempfile.TemporaryDirectory() as tmp:
            bogus = pathlib.Path(tmp) / "does-not-exist-manifest-root"
            result = self.run_script(bogus)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("error:", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(bogus), result.stderr)

    def test_scan_onerror_failure_leaves_manifest_unchanged(self):
        """An injected os.fwalk onerror failure aborts and leaves existing manifest unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "file.txt").write_text("x", encoding="utf-8")
            manifest = root / "MANIFEST.json"
            manifest.write_text('{"files":[{"path":"old.txt","sha256":"' + "a" * 64 + '","bytes":1}]}', encoding="utf-8")

            def failing_fwalk(path, *args, **kwargs):
                onerror = kwargs.get("onerror")
                if onerror is not None:
                    onerror(OSError(errno.EACCES, "permission denied", str(path)))
                return iter(())

            stderr_capture = io.StringIO()
            with mock.patch("tools.recalc_manifest.os.fwalk", side_effect=failing_fwalk):
                with mock.patch("sys.argv", ["recalc_manifest.py", "--root", str(root)]):
                    with mock.patch("sys.stderr", stderr_capture):
                        rc = recalc.main()
            self.assertNotEqual(rc, 0)
            stderr_text = stderr_capture.getvalue()
            self.assertIn("error:", stderr_text.lower())
            self.assertNotIn("Traceback", stderr_text)
            self.assertNotIn(str(root), stderr_text)
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"files":[{"path":"old.txt","sha256":"' + "a" * 64 + '","bytes":1}]}')

    def test_empty_scan_cannot_erase_nonempty_manifest(self):
        """A scan that finds no included files refuses to overwrite a nonempty existing manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            manifest = root / "MANIFEST.json"
            manifest.write_text('{"files":[{"path":"old.txt","sha256":"' + "a" * 64 + '","bytes":1}]}', encoding="utf-8")
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("empty", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"files":[{"path":"old.txt","sha256":"' + "a" * 64 + '","bytes":1}]}')

    def test_symlink_inside_skipped_runtime_dir_fails_closed(self):
        """A symlink inside a skipped runtime directory is detected and aborts the run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = root / ".runtime-state"
            runtime.mkdir()
            real = runtime / "real.txt"
            real.write_text("data", encoding="utf-8")
            link = runtime / "link.txt"
            link.symlink_to(real)
            result = self.run_script(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("link.txt", result.stderr)
            self.assertFalse((root / "MANIFEST.json").exists())

    def test_open_nofollow_rejects_fifo_race(self):
        """_open_nofollow refuses to open a FIFO (regular-file-to-special race), if creatable."""
        with tempfile.TemporaryDirectory() as tmp:
            fifo = pathlib.Path(tmp) / "fifo"
            try:
                os.mkfifo(fifo)
            except (AttributeError, OSError):
                self.skipTest("platform cannot create FIFO")
            with self.assertRaises(OSError):
                recalc._open_nofollow(fifo)


if __name__ == "__main__":
    unittest.main()
