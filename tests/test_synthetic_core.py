from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.alerts import build_alert, write_alert  # noqa: E402
from case_search.diff import diff_snapshots  # noqa: E402
from case_search.errors import (MalformedPayloadError,  # noqa: E402
                                  SourceUnavailableError, StorageError,
                                  WafDetectedError)
from case_search.pipeline import fetch  # noqa: E402
from case_search.snapshots import watch_case  # noqa: E402
from case_search.sources import build_provider  # noqa: E402
from case_search.sources.base import (RawSourceResult,  # noqa: E402
                                        classify)
from case_search.sources.live import (KadProvider, MosGorsudProvider,  # noqa: E402
                                        SudrfProvider)
from case_search.sources.synthetic import SyntheticProvider  # noqa: E402
from case_search.storage import (SnapshotStore,  # noqa: E402
                                   safe_slug, snapshot_from_dict)

FIXTURES = REPO_ROOT / "fixtures" / "synthetic"
NOW = "2026-09-06T00:00:00Z"


def synth_provider() -> object:
    return build_provider("synthetic", FIXTURES)


class HappyPathTests(unittest.TestCase):
    def _check_provenance(self, snapshot):
        prov = snapshot.provenance
        self.assertTrue(prov.source)
        self.assertTrue(prov.source_url)
        self.assertEqual(prov.retrieved_at, NOW)
        self.assertTrue(prov.parser_version)
        self.assertEqual(len(prov.raw_artifact_hash), 64)

    def test_kad_case_normalizes(self):
        outcome = fetch(synth_provider(), "A40-SYNTH-2026", NOW)
        self.assertEqual(outcome.status, "ok")
        snap = outcome.snapshot
        self.assertIsNotNone(snap)
        self.assertEqual(snap.case_number, "A40-SYNTH-2026")
        self.assertEqual(snap.parties.plaintiff, ("ООО Синтетика",))
        self.assertEqual(snap.next_hearing.hall, "4012")
        self.assertEqual(len(snap.events_timeline), 1)
        self._check_provenance(snap)

    def test_mos_gorsud_civil_normalizes(self):
        outcome = fetch(synth_provider(), "02-1234/2026", NOW)
        self.assertEqual(outcome.status, "ok")
        snap = outcome.snapshot
        self.assertEqual(snap.parties.third_parties, ("Третье лицо СИНА",))
        self.assertFalse(snap.events_timeline[1].published)
        self._check_provenance(snap)

    def test_deterministic_serialization(self):
        a = fetch(synth_provider(), "A40-SYNTH-2026", NOW).snapshot
        b = fetch(synth_provider(), "A40-SYNTH-2026", NOW).snapshot
        self.assertEqual(a.to_json(), b.to_json())
        self.assertEqual(a.content_hash(), b.content_hash())


class FailClosedTests(unittest.TestCase):
    def test_waf_is_degraded_never_snapshot(self):
        outcome = fetch(synth_provider(), "SYNTH-WAF", NOW)
        self.assertEqual(outcome.status, "degraded")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("captcha_or_waf", outcome.reason)

    def test_rate_limit_is_degraded_with_retry_after(self):
        outcome = fetch(synth_provider(), "SYNTH-RATELIMIT", NOW)
        self.assertEqual(outcome.status, "degraded")
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(outcome.retry_after, 900.0)

    def test_empty_body_is_error(self):
        outcome = fetch(synth_provider(), "SYNTH-EMPTY", NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("empty payload", outcome.reason)

    def test_malformed_json_is_error(self):
        outcome = fetch(synth_provider(), "SYNTH-MALFORMED", NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)

    def test_bad_schema_payload_is_error(self):
        outcome = fetch(synth_provider(), "SYNTH-BADSCHEMA", NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("participants", outcome.reason)

    def test_http_500_is_error(self):
        outcome = fetch(synth_provider(), "SYNTH-HTTP500", NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)

    def test_unknown_case_ref_is_error_not_empty_success(self):
        outcome = fetch(synth_provider(), "NOPE-0000", NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)

    def test_classify_rejects_json_null_body_as_empty(self):
        from case_search.errors import EmptyPayloadError
        raw = RawSourceResult("synthetic-x", "file://x", 200, "null")
        with self.assertRaises(EmptyPayloadError):
            classify(raw)

    def test_normalize_rejects_missing_fields(self):
        with self.assertRaises(MalformedPayloadError):
            from case_search.normalize import normalize
            normalize({"case_number": "X"}, source="s", source_url="u",
                      retrieved_at=NOW, raw_artifact_hash="h")


class LiveProviderQuarantineTests(unittest.TestCase):
    def test_live_adapters_refuse_lookup(self):
        for provider in (KadProvider(), MosGorsudProvider(),
                         SudrfProvider()):
            with self.assertRaises(SourceUnavailableError) as ctx:
                provider.lookup("anything")
            self.assertIn("NOT_IMPLEMENTED", str(ctx.exception))

    def test_fetch_against_quarantined_provider_is_error(self):
        outcome = fetch(KadProvider(), "A40-REAL-NOPE", NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.base = fetch(synth_provider(), "A40-SYNTH-2026",
                          NOW).snapshot

    def test_no_diff_on_identical_snapshots(self):
        again = fetch(synth_provider(), "A40-SYNTH-2026", NOW).snapshot
        self.assertEqual(diff_snapshots(self.base, again), [])

    def test_detects_status_and_hearing_change(self):
        data = self.base.to_dict()
        data["status"] = "Завершено"
        data["next_hearing"]["date"] = "2026-12-01"
        from case_search.storage import snapshot_from_dict
        changed = snapshot_from_dict(data)
        paths = [c.path for c in diff_snapshots(self.base, changed)]
        self.assertEqual(paths, ["next_hearing.date", "status"])

    def test_detects_event_publication_change(self):
        data = self.base.to_dict()
        data["events_timeline"][0]["published"] = False
        from case_search.storage import snapshot_from_dict
        changed = snapshot_from_dict(data)
        changes = diff_snapshots(self.base, changed)
        self.assertEqual(len(changes), 1)
        self.assertIn("event[2026-09-01|", changes[0].path)
        self.assertEqual((changes[0].before, changes[0].after), (True, False))

    def test_diff_deterministic_and_sorted(self):
        data = self.base.to_dict()
        data["status"] = "X"
        data["judge"] = "Y"
        data["events_timeline"].append({"date": "2026-09-05",
                                        "name": "Синтетика",
                                        "published": True})
        from case_search.storage import snapshot_from_dict
        changed = snapshot_from_dict(data)
        first = diff_snapshots(self.base, changed)
        second = diff_snapshots(self.base, changed)
        self.assertEqual([c.to_dict() for c in first],
                         [c.to_dict() for c in second])
        self.assertEqual([c.path for c in first],
                         sorted(c.path for c in first))


class StorageTests(unittest.TestCase):
    def test_save_load_roundtrip_and_baseline_preserved_on_failure(self):
        snap = fetch(synth_provider(), "A40-SYNTH-2026", NOW).snapshot
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            saved = store.save(snap)
            self.assertTrue(saved.path.is_file())
            loaded = store.load_latest(snap.case_number)
            self.assertEqual(loaded.content_hash(), snap.content_hash())

            outcome = fetch(synth_provider(), "SYNTH-WAF", NOW)
            result = watch_case(store, outcome)
            self.assertTrue(result.baseline_preserved)
            still = store.load_latest(snap.case_number)
            self.assertEqual(still.content_hash(), snap.content_hash())

    def test_watch_twice_then_change(self):
        provider = synth_provider()
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            first = watch_case(store, fetch(provider, "A40-SYNTH-2026", NOW))
            self.assertFalse(first.changed)
            second = watch_case(store, fetch(provider, "A40-SYNTH-2026", NOW))
            self.assertFalse(second.changed)

            data = json.loads(first.stored.path.read_text(encoding="utf-8"))
            data["status"] = "Отложено"
            from case_search.storage import snapshot_from_dict
            mutated = snapshot_from_dict(data)
            third = watch_case(store, _OkOutcome(mutated))
            self.assertTrue(third.changed)
            self.assertEqual([c.path for c in third.changes], ["status"])


class _OkOutcome:
    status = "ok"
    reason = "test"
    retry_after = None

    def __init__(self, snapshot):
        self.snapshot = snapshot


class AlertTests(unittest.TestCase):
    def test_degraded_outcome_writes_local_alert(self):
        outcome = fetch(synth_provider(), "SYNTH-WAF", NOW)
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            result = watch_case(store, outcome)
            alert = build_alert("SYNTH-WAF", result, now=NOW)
            self.assertIsNotNone(alert)
            self.assertEqual(alert.status, "degraded")
            self.assertEqual(alert.destination, "local-file")
            self.assertEqual(alert.created_at, NOW)
            path = write_alert(alert, Path(tmp) / "alerts")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "degraded")
            self.assertEqual(data["created_at"], NOW)

    def test_unchanged_snapshot_produces_no_alert(self):
        outcome = fetch(synth_provider(), "A40-SYNTH-2026", NOW)
        with tempfile.TemporaryDirectory() as tmp:
            result = watch_case(SnapshotStore(tmp), outcome)
            self.assertIsNone(build_alert("A40-SYNTH-2026", result))

    def test_distinct_events_never_clobber_each_other(self):
        degraded = watch_case(SnapshotStore(tempfile.mkdtemp()),
                              fetch(synth_provider(), "SYNTH-WAF", NOW))
        errored = watch_case(SnapshotStore(tempfile.mkdtemp()),
                             fetch(synth_provider(), "SYNTH-EMPTY", NOW))
        with tempfile.TemporaryDirectory() as tmp:
            first = write_alert(build_alert("CASE", degraded, now=NOW),
                                tmp)
            second = write_alert(build_alert("CASE", errored, now=NOW),
                                 tmp)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())


class ProvenanceSchemaTests(unittest.TestCase):
    def test_empty_provenance_fields_rejected(self):
        from case_search.schema import Provenance
        with self.assertRaises(ValueError):
            Provenance(source="", source_url="u", retrieved_at=NOW,
                       raw_artifact_hash="h")
        with self.assertRaises(ValueError):
            Provenance(source="s", source_url="u", retrieved_at=NOW,
                       raw_artifact_hash="")
        with self.assertRaises(ValueError):
            Provenance(source="s", source_url="u", retrieved_at="   ",
                       raw_artifact_hash="h")

    def test_normalize_rejects_empty_provenance_values(self):
        from case_search.normalize import normalize
        with self.assertRaises(ValueError):
            normalize({"case_number": "X", "court": "c", "status": "s",
                       "updated_at": "t"},
                      source="", source_url="u", retrieved_at=NOW,
                      raw_artifact_hash="h")

    def test_empty_snapshot_fields_rejected(self):
        from case_search.normalize import normalize
        with self.assertRaises(MalformedPayloadError):
            normalize({"case_number": "  ", "court": "c", "status": "s",
                       "updated_at": "t"},
                      source="s", source_url="u", retrieved_at=NOW,
                      raw_artifact_hash="h")

    def test_duplicate_timeline_events_rejected(self):
        from case_search.normalize import normalize
        payload = json.loads(
            (FIXTURES / "kad_case.json").read_text(encoding="utf-8"))
        payload["events_timeline"].append(
            dict(payload["events_timeline"][0]))
        with self.assertRaises(MalformedPayloadError) as ctx:
            normalize(payload, source="s", source_url="u",
                      retrieved_at=NOW, raw_artifact_hash="h")
        self.assertIn("duplicate timeline event", str(ctx.exception))


class FailClosedHardeningTests(unittest.TestCase):
    def test_classify_drops_non_positive_retry_after(self):
        raw = RawSourceResult("s", "u", 429, "rate limited",
                              error="rate_limited", retry_after=-5)
        with self.assertRaises(WafDetectedError) as ctx:
            classify(raw)
        self.assertIsNone(ctx.exception.retry_after)

    def test_fixture_retry_after_junk_is_none(self):
        self.assertIsNone(SyntheticProvider._fixture_retry_after(
            '{"status_code":429,"retry_after":"soon"}'))
        self.assertIsNone(SyntheticProvider._fixture_retry_after(
            '{"status_code":429,"retry_after":-1}'))

    def test_no_unexplained_waf_markers(self):
        from case_search.sources.base import WAF_MARKERS
        for marker in WAF_MARKERS:
            self.assertGreater(len(marker), 2)
            self.assertNotIn("juick", marker)


class StorageIntegrityTests(unittest.TestCase):
    def _store_with_snapshot(self, tmp):
        store = SnapshotStore(tmp)
        snap = fetch(synth_provider(), "A40-SYNTH-2026", NOW).snapshot
        store.save(snap)
        return store, snap

    def test_corrupt_pointer_raises_storage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, snap = self._store_with_snapshot(tmp)
            pointer = Path(tmp) / safe_slug(snap.case_number) / "latest.json"
            pointer.write_text("{bogus", encoding="utf-8")
            with self.assertRaises(StorageError):
                store.load_latest(snap.case_number)

    def test_traversal_pointer_entry_raises_storage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, snap = self._store_with_snapshot(tmp)
            pointer = Path(tmp) / safe_slug(snap.case_number) / "latest.json"
            pointer.write_text(json.dumps(
                {"content_hash": "h", "file": "../../etc/passwd"}),
                encoding="utf-8")
            with self.assertRaises(StorageError):
                store.load_latest(snap.case_number)

    def test_hash_mismatch_raises_storage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, snap = self._store_with_snapshot(tmp)
            case_dir = Path(tmp) / safe_slug(snap.case_number)
            pointer = case_dir / "latest.json"
            meta = json.loads(pointer.read_text(encoding="utf-8"))
            meta["content_hash"] = "0" * 64
            pointer.write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaises(StorageError):
                store.load_latest(snap.case_number)

    def test_tampered_snapshot_body_raises_storage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, snap = self._store_with_snapshot(tmp)
            case_dir = Path(tmp) / safe_slug(snap.case_number)
            snapfile = next(p for p in case_dir.iterdir()
                            if p.name.endswith(".json")
                            and p.name != "latest.json")
            data = json.loads(snapfile.read_text(encoding="utf-8"))
            data["status"] = "tampered"
            snapfile.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(StorageError):
                store.load_latest(snap.case_number)

    def test_snapshot_from_dict_invalid_raises_storage_error(self):
        with self.assertRaises(StorageError):
            snapshot_from_dict({})
        with self.assertRaises(StorageError):
            snapshot_from_dict({"unexpected": "shape"})

    def test_missing_pointer_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                SnapshotStore(tmp).load_latest("NEVER-SEEN"))


class CliTests(unittest.TestCase):
    def _run(self, argv):
        from case_search.cli import main
        return main(argv)

    def test_lookup_ok_exit_0_and_deterministic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--fixtures", str(FIXTURES),
                    "--store", f"{tmp}/store", "--alerts", f"{tmp}/alerts",
                    "--now", NOW]
            rc1 = self._run(base + ["lookup", "A40-SYNTH-2026",
                                    "--json", f"{tmp}/a.json"])
            rc2 = self._run(base + ["lookup", "A40-SYNTH-2026",
                                    "--json", f"{tmp}/b.json"])
            self.assertEqual((rc1, rc2), (0, 0))
            self.assertEqual(
                Path(f"{tmp}/a.json").read_bytes(),
                Path(f"{tmp}/b.json").read_bytes())
            self.assertTrue(list(Path(f"{tmp}/store").rglob("latest.json")))

    def test_lookup_waf_exit_2_alert_written_no_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(["--fixtures", str(FIXTURES),
                            "--store", f"{tmp}/store",
                            "--alerts", f"{tmp}/alerts", "--now", NOW,
                            "lookup", "SYNTH-WAF"])
            self.assertEqual(rc, 2)
            alerts = list(Path(f"{tmp}/alerts").rglob("*.json"))
            self.assertEqual(len(alerts), 1)
            self.assertFalse(
                list(Path(f"{tmp}/store").rglob("latest.json")))

    def test_lookup_empty_payload_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(["--fixtures", str(FIXTURES),
                            "--store", f"{tmp}/store",
                            "--alerts", f"{tmp}/alerts", "--now", NOW,
                            "lookup", "SYNTH-EMPTY"])
            self.assertEqual(rc, 3)

    def test_lookup_bad_schema_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(["--fixtures", str(FIXTURES),
                            "--store", f"{tmp}/store",
                            "--alerts", f"{tmp}/alerts", "--now", NOW,
                            "lookup", "SYNTH-BADSCHEMA"])
            self.assertEqual(rc, 3)

    def test_watch_bad_watchlist_exit_3_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("not-json", encoding="utf-8")
            rc = self._run(["--fixtures", str(FIXTURES),
                            "--store", f"{tmp}/store",
                            "--alerts", f"{tmp}/alerts", "--now", NOW,
                            "watch", str(bad)])
            self.assertEqual(rc, 3)

    def test_watch_synthetic_watchlist_mixed_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(["--fixtures", str(FIXTURES),
                            "--store", f"{tmp}/store",
                            "--alerts", f"{tmp}/alerts", "--now", NOW,
                            "watch", str(FIXTURES / "watchlist.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(len(list(
                Path(f"{tmp}/store").rglob("latest.json"))), 2)
            alert_files = [
                p for p in Path(f"{tmp}/alerts").rglob("*.json")
                if p.name != "seen_fingerprints.json"
            ]
            self.assertEqual(len(alert_files), 1)

    def test_providers_exit_0_reports_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(["--store", f"{tmp}/s", "--alerts", f"{tmp}/a",
                            "providers"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
