from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.alerts import Alert, build_alert  # noqa: E402
from case_search.alerts_dedup import (  # noqa: E402
    alert_fingerprint,
    filter_seen,
    is_duplicate,
    load_seen_fingerprints,
    record_fingerprints,
)
from case_search.pipeline import fetch  # noqa: E402
from case_search.snapshots import watch_case  # noqa: E402
from case_search.sources import build_provider  # noqa: E402
from case_search.storage import SnapshotStore  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures" / "synthetic"
NOW1 = "2026-09-06T00:00:00Z"
NOW2 = "2026-09-07T12:00:00Z"


def _synth_provider():
    return build_provider("synthetic", str(FIXTURES))


def _alert_files(alerts_dir: Path) -> list[Path]:
    return [
        p for p in alerts_dir.rglob("*.json")
        if p.name != "seen_fingerprints.json"
    ]


class AlertFingerprintTests(unittest.TestCase):
    def test_fingerprint_stable_across_timestamps_for_degraded_alert(self):
        result = watch_case(
            SnapshotStore(tempfile.mkdtemp()),
            fetch(_synth_provider(), "SYNTH-WAF", NOW1),
        )
        a1 = build_alert("SYNTH-WAF", result, now=NOW1)
        a2 = build_alert("SYNTH-WAF", result, now=NOW2)
        self.assertIsNotNone(a1)
        self.assertIsNotNone(a2)
        self.assertNotEqual(a1.created_at, a2.created_at)
        self.assertEqual(
            alert_fingerprint("SYNTH-WAF", result),
            alert_fingerprint("SYNTH-WAF", a1),
        )
        self.assertEqual(
            alert_fingerprint("SYNTH-WAF", a1),
            alert_fingerprint("SYNTH-WAF", a2),
        )

    def test_fingerprint_differs_for_different_changes(self):
        a1 = Alert(
            case_number="CASE-1",
            status="changed",
            reason="snapshot diff detected",
            changes=[{"path": "status", "before": "old", "after": "new"}],
            destination="local-file",
            created_at=NOW1,
        )
        a2 = Alert(
            case_number="CASE-1",
            status="changed",
            reason="snapshot diff detected",
            changes=[{"path": "status", "before": "old", "after": "other"}],
            destination="local-file",
            created_at=NOW1,
        )
        self.assertNotEqual(
            alert_fingerprint("CASE-1", a1),
            alert_fingerprint("CASE-1", a2),
        )

    def test_fingerprint_differs_for_different_reasons(self):
        a1 = Alert(
            case_number="CASE-1",
            status="error",
            reason="network failure",
            changes=[],
            destination="local-file",
            created_at=NOW1,
        )
        a2 = Alert(
            case_number="CASE-1",
            status="error",
            reason="empty payload",
            changes=[],
            destination="local-file",
            created_at=NOW1,
        )
        self.assertNotEqual(
            alert_fingerprint("CASE-1", a1),
            alert_fingerprint("CASE-1", a2),
        )


class SeenFingerprintsStoreTests(unittest.TestCase):
    def test_record_load_and_is_duplicate_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            record_fingerprints(path, {"abc": NOW1, "def": NOW2})
            loaded = load_seen_fingerprints(path)
            self.assertEqual(loaded, {"abc": NOW1, "def": NOW2})
            self.assertTrue(is_duplicate(path, "abc"))
            self.assertFalse(is_duplicate(path, "xyz"))

    def test_corrupt_store_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertLogs("case_search.alerts_dedup", level="WARNING"):
                self.assertEqual(load_seen_fingerprints(path), {})
            self.assertFalse(is_duplicate(path, "any"))

    def test_filter_seen_counts_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            a_known = Alert("K", "changed", "snapshot diff detected", [],
                            "local-file", NOW2)
            a_new = Alert("N", "changed", "snapshot diff detected", [],
                          "local-file", NOW2)
            known_fp = alert_fingerprint("K", a_known)
            record_fingerprints(path, {known_fp: NOW1})
            new_alerts, dup_count = filter_seen(path, [a_known, a_new])
            self.assertEqual(dup_count, 1)
            self.assertEqual([a.case_number for a in new_alerts], ["N"])


class WatchDedupIntegrationTests(unittest.TestCase):
    def _run_watch(self, tmp: str, watchlist: Path, *, no_dedup: bool = False,
                   now: str = NOW1):
        from case_search.cli import cmd_watch  # noqa: E402
        import argparse
        args = argparse.Namespace(
            watchlist=str(watchlist),
            source=None,
            fixtures=str(FIXTURES),
            store=f"{tmp}/store",
            alerts=f"{tmp}/alerts",
            now=now,
            no_dedup=no_dedup,
        )
        return cmd_watch(args)

    def test_second_watch_run_suppresses_identical_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = FIXTURES / "watchlist.json"

            rc1 = self._run_watch(tmp, watchlist)
            self.assertEqual(rc1, 2)
            self.assertEqual(len(_alert_files(Path(f"{tmp}/alerts"))), 1)
            seen_path = Path(f"{tmp}/alerts") / "seen_fingerprints.json"
            self.assertTrue(seen_path.is_file())
            self.assertEqual(len(load_seen_fingerprints(seen_path)), 1)

            rc2 = self._run_watch(tmp, watchlist)
            self.assertEqual(rc2, 2)
            self.assertEqual(len(_alert_files(Path(f"{tmp}/alerts"))), 1)
            self.assertEqual(len(load_seen_fingerprints(seen_path)), 1)

    def test_no_dedup_flag_allows_duplicate_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = FIXTURES / "watchlist.json"

            rc1 = self._run_watch(tmp, watchlist)
            self.assertEqual(rc1, 2)
            self.assertEqual(len(_alert_files(Path(f"{tmp}/alerts"))), 1)

            rc2 = self._run_watch(tmp, watchlist, no_dedup=True, now=NOW2)
            self.assertEqual(rc2, 2)
            self.assertEqual(len(_alert_files(Path(f"{tmp}/alerts"))), 2)


if __name__ == "__main__":
    unittest.main()
