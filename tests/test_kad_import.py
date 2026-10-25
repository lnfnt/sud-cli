"""KAD import mode tests: saved live extract -> parse -> snapshot."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (MalformedPayloadError,  # noqa: E402
                                  SourceUnavailableError)
from case_search.kad.import_json import KadImportProvider  # noqa: E402
from case_search.kad.parser import fetch_kad_html  # noqa: E402

LIVE_EXTRACT = (REPO_ROOT / "fixtures"
                / "kad-live-extract-А40-308035-2025.json")


def _stage(tmp: str, payload) -> str:
    path = Path(tmp) / "kad-extract.json"
    path.write_text(json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")
    return str(path)


class KadImportProviderTests(unittest.TestCase):
    def test_live_extract_fixture_parses(self):
        """The saved live EXTRACT_EXPRESSION output parses end-to-end."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = _stage(tmp, json.loads(
                LIVE_EXTRACT.read_text(encoding="utf-8")))
            provider = KadImportProvider(stage, "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "ok")
        s = out.snapshot
        self.assertEqual(s.case_number, "А40-308035/2025")
        self.assertEqual(s.court, "АС города Москвы")
        self.assertEqual(s.judge, "Шуваева М. В.")
        self.assertEqual(s.parties.plaintiff, ("POD Trading LLC",))
        self.assertEqual(s.parties.respondent, ("Interactive Brokers LLC",))
        self.assertEqual(s.next_hearing.date, "2026-10-21")
        self.assertEqual(s.provenance.source, "kad-import:kad-extract.json")

    def test_double_encoded_string_unwrapped(self):
        raw = LIVE_EXTRACT.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            stage = _stage(tmp, raw)  # JSON-encoded string payload
            provider = KadImportProvider(stage, "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "ok")

    def test_wrong_staged_ref_fails_closed_no_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = KadImportProvider(str(Path(tmp) / "x.json"),
                                         "А40-308035/2025")
            with self.assertRaises(SourceUnavailableError):
                provider.lookup("А40-999/2025")

    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = KadImportProvider(
                str(Path(tmp) / "missing.json"), "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "error")
        self.assertIn("missing", out.reason)

    def test_malformed_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "bad.json"
            stage.write_text("{not json", encoding="utf-8")
            provider = KadImportProvider(str(stage), "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "error")
        self.assertIn("not valid JSON", out.reason)

    def test_non_object_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = _stage(tmp, [1, 2, 3])
            provider = KadImportProvider(stage, "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "error")
        self.assertIn("JSON object", out.reason)

    def test_junk_string_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = _stage(tmp, "just a plain string")
            provider = KadImportProvider(stage, "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "error")

    def test_identity_mismatch_is_error(self):
        data = json.loads(LIVE_EXTRACT.read_text(encoding="utf-8"))
        data["case_number"] = "А40-111/2025"
        with tempfile.TemporaryDirectory() as tmp:
            stage = _stage(tmp, data)
            provider = KadImportProvider(stage, "А40-308035/2025")
            out = fetch_kad_html(provider, "А40-308035/2025",
                                 "2026-09-09T00:00:00Z")
        self.assertEqual(out.status, "error")
        self.assertIn("identity mismatch", out.reason)


if __name__ == "__main__":
    unittest.main()
