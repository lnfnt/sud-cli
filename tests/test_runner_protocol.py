import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))
from runner_protocol import parse_request, response_json


class RunnerProtocolTests(unittest.TestCase):
    def test_request_allows_only_mos(self):
        req = parse_request('{"case_ref":"02-0387/2025"}')
        self.assertEqual(req.source, "mos-gorsud")
        with self.assertRaises(ValueError):
            parse_request('{"case_ref":"x","source":"kad"}')

    def test_response_is_single_json_object(self):
        line = response_json(__import__("runner_protocol").RunnerResponse(
            "error", "x", reason="blocked"))
        self.assertEqual(json.loads(line)["status"], "error")

    def test_runner_fail_closed(self):
        p = subprocess.run(
            [sys.executable, str(ROOT / "case_search_runner.py"),
             "lookup", "02-0387/2025", "--output", "/tmp/out.json"],
            text=True, capture_output=True, check=False)
        self.assertIn(p.returncode, (2, 3))
        obj = json.loads(p.stdout)
        self.assertIn(obj["status"], ("error", "degraded"))
        self.assertIsNone(obj["snapshot"])


if __name__ == "__main__":
    unittest.main()
