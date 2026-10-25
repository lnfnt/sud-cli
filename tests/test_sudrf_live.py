"""SudRF live adapter tests (fake HTTP, no network)."""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.sudrf import live  # noqa: E402
from case_search.sudrf.live import (SudrfLiveError,  # noqa: E402
                                      fetch_listing, get_live_case,
                                      parse_live_card_header,
                                      parse_live_motion,
                                      parse_live_participants,
                                      resolve_host)

CARD = "<!--" + "x" * 11000 + "-->""" + """<html><body><table id='tablcont'><tr><th>ДЕЛО</th></tr>
<tr><td><b>Уникальный идентификатор дела</b></td><td>50RS0019-01-2025-000294-14</td></tr>
<tr><td><b>Дата поступления</b></td><td>17.07.2026</td></tr>
<tr><td><b>Категория дела</b></td><td>Споры семейные</td></tr>
<tr><td><b>Судья</b></td><td>Тюшляева Н.В.</td></tr>
</table>
<table><tr><th>ДВИЖЕНИЕ ДЕЛА</th></tr>
<tr><td align='center'><b>Наименование события</b></td><td><b>Дата</b></td><td><b>Время</b></td></tr>
<tr><td>Передача дела судье</td><td>21.07.2026</td><td>17:43</td></tr>
<tr><td>Судебное заседание</td><td>16.09.2026</td><td>09:20</td></tr>
</table>
<table><tr><th>УЧАСТНИКИ</th></tr>
<tr><td><b>Вид лица, участвующего в деле</b></td><td><b>Фамилия</b></td></tr>
<tr><td>ИСТЕЦ</td><td>Информация скрыта</td></tr>
<tr><td>ОТВЕТЧИК</td><td>ООО Ромашка</td></tr>
</table></body></html>"""

LISTING = """<a href='/modules.php?name=sud_delo&srv_num=1&name_op=case&case_id=268838394&case_uid=a63d066e-b906-42e1-80e2-0dfa9dc8ea21&delo_id=5&new=5'><u>33-35073/2026</u></a><td>09:20</td>"""


class FakeHttp:
    """Patch _http_get to return canned bodies per URL fragment."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url, *, proxy=None, timeout=30):
        self.calls.append(url)
        if "H_date=" in url:
            return 200, LISTING
        if "name_op=case" in url:
            return 200, CARD
        return 200, LISTING


class ResolverTests(unittest.TestCase):
    def test_registry_has_hosts(self):
        hosts = live.list_registry_hosts()
        self.assertGreaterEqual(len(hosts), 14)
        self.assertIn("oblsud.mo.sudrf.ru", hosts)

    def test_resolve_host_both_schemes(self):
        cands = resolve_host("oblsud", "mo")
        self.assertIn("oblsud.mo.sudrf.ru", cands)
        self.assertIn("oblsud--mo.sudrf.ru", cands)


class MotionTests(unittest.TestCase):
    def test_parse_motion(self):
        events = parse_live_motion(CARD)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "Передача дела судье")
        self.assertEqual(events[0]["date"], "21.07.2026")
        self.assertEqual(events[1]["event"], "Судебное заседание")

    def test_parse_motion_missing_table(self):
        self.assertEqual(parse_live_motion("<html>no table</html>"), [])

    def test_parse_participants(self):
        parts = parse_live_participants(CARD)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["role"], "ИСТЕЦ")
        self.assertEqual(parts[0]["masked"], "true")
        self.assertEqual(parts[1]["name"], "ООО Ромашка")
        self.assertEqual(parts[1]["masked"], "false")

    def test_parse_header(self):
        hdr = parse_live_card_header(CARD)
        self.assertEqual(hdr["Уникальный идентификатор дела"],
                         "50RS0019-01-2025-000294-14")
        self.assertEqual(hdr["Судья"], "Тюшляева Н.В.")


class GetLiveCaseTests(unittest.TestCase):
    def test_full_pipeline(self):
        with patch.object(live, "_http_get", FakeHttp()):
            result = get_live_case("33-35073/2026", host="oblsud.mo.sudrf.ru")
        self.assertEqual(result["case_number"], "33-35073/2026")
        self.assertEqual(result["uid"], "50RS0019-01-2025-000294-14")
        self.assertEqual(len(result["motion"]), 2)
        self.assertEqual(len(result["participants"]), 2)
        self.assertIn("case_uid=a63d066e", result["card_url"])

    def test_not_listed_raises(self):
        class Empty(FakeHttp):
            def __call__(self, url, **kw):
                return 200, "<html>empty</html>"
        with patch.object(live, "_http_get", Empty()):
            with self.assertRaises(SudrfLiveError) as ctx:
                get_live_case("99-9999/2030", host="oblsud.mo.sudrf.ru")
        self.assertIn("not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
