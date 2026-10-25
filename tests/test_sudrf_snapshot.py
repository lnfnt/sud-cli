"""SudRF live result -> canonical CaseSnapshot tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import MalformedPayloadError  # noqa: E402
from case_search.schema import CaseSnapshot  # noqa: E402
from case_search.sudrf.live import sudrf_result_to_snapshot  # noqa: E402


class SudrfResultToSnapshotTests(unittest.TestCase):
    def _sample_result(self) -> dict:
        return {
            "case_number": "33-35073/2026",
            "court_name": "Московский областной суд",
            "host": "oblsud.mo.sudrf.ru",
            "card_url": "https://oblsud.mo.sudrf.ru/card",
            "header": {
                "Судья": "Тюшляева Н.В.",
                "Результат рассмотрения": "Передано судье",
            },
            "participants": [
                {"role": "ИСТЕЦ", "name": "Иванов И.И.",
                 "masked": "false"},
                {"role": "ОТВЕТЧИК", "name": "ООО Ромашка",
                 "masked": "false"},
            ],
            "motion": [
                {"event": "Передача дела судье", "date": "21.07.2026"},
                {"event": "Судебное заседание", "date": "16.09.2026"},
            ],
        }

    def test_builds_snapshot(self):
        result = self._sample_result()
        snapshot = sudrf_result_to_snapshot(result)

        self.assertIsInstance(snapshot, CaseSnapshot)
        self.assertEqual(snapshot.case_number, "33-35073/2026")
        self.assertEqual(snapshot.court, "Московский областной суд")
        self.assertEqual(snapshot.judge, "Тюшляева Н.В.")
        self.assertEqual(snapshot.status, "Передано судье")
        self.assertEqual(snapshot.parties.plaintiff, ("Иванов И.И.",))
        self.assertEqual(snapshot.parties.respondent, ("ООО Ромашка",))
        self.assertEqual(len(snapshot.events_timeline), 2)
        self.assertEqual(snapshot.events_timeline[0].date, "2026-07-21")
        self.assertEqual(snapshot.events_timeline[0].name,
                         "Передача дела судье")
        self.assertTrue(snapshot.events_timeline[0].published)
        self.assertIsNotNone(snapshot.last_event)
        self.assertEqual(snapshot.last_event.date, "2026-07-21")

    def test_missing_case_number_raises(self):
        result = self._sample_result()
        del result["case_number"]
        with self.assertRaises(MalformedPayloadError) as ctx:
            sudrf_result_to_snapshot(result)
        self.assertIn("case_number", str(ctx.exception))

    def test_unknown_role_mapped_to_third_parties(self):
        result = self._sample_result()
        result["participants"].append(
            {"role": "Прокурор", "name": "Петров П.П."})
        snapshot = sudrf_result_to_snapshot(result)
        self.assertIn("Петров П.П.", snapshot.parties.third_parties)
        self.assertTrue(any("unmapped participant role" in w
                            for w in snapshot.provenance.warnings))


if __name__ == "__main__":
    unittest.main()
