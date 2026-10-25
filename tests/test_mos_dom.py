"""Replay tests for the bounded mos-gorsud DOM parser.

Synthetic fixtures only: no network, no real case data. The happy fixture is
the saved, PII-redacted public card; failure fixtures cover empty, WAF,
missing summary and malformed/ambiguous HTML.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import (EmptyPayloadError,  # noqa: E402
                                  MalformedPayloadError,
                                  SourceUnavailableError, WafDetectedError)
from case_search.mos import parser as mos_parser  # noqa: E402
from case_search.mos.parser import (MOS_DOM_PARSER_VERSION,  # noqa: E402
                                      classify_html, parse_mos_html)
from case_search.pipeline import fetch  # noqa: E402
from case_search.snapshots import watch_case  # noqa: E402
from case_search.sources import build_provider  # noqa: E402
from case_search.sources.base import RawSourceResult, classify  # noqa: E402
from case_search.sources.live import MosGorsudProvider  # noqa: E402
from case_search.sources.mos_dom import MosDomReplayProvider  # noqa: E402
from case_search.storage import SnapshotStore  # noqa: E402

MOS_FIXTURES = REPO_ROOT / "mos-spike" / "fixtures"
HAPPY_FIXTURE = MOS_FIXTURES / "case-02-0387-2025.redacted.html"
HAPPY_REF = "02-0387/2025"
NOW = "2026-09-06T00:00:00Z"


def mos_provider() -> MosDomReplayProvider:
    return build_provider("mos-gorsud-dom", MOS_FIXTURES)


def happy_html() -> str:
    return HAPPY_FIXTURE.read_text(encoding="utf-8")


def parse_happy() -> mos_parser.MosRecord:
    return parse_mos_html(happy_html(), source="mos-gorsud-dom:test",
                          source_url="file://test", retrieved_at=NOW)


def _row(label: str, value: str) -> str:
    return (f'<div class="row"><div class="left">{label}</div>'
            f'<div class="right">{value}</div></div>')


def _tr(label: str, value: str) -> str:
    return f"<tr><td>{label}</td><td>{value}</td></tr>"


def card_html(summary_rows=None, timeline_rows=None, hearings_rows=None,
              h1="Информация по делу № 02-1234/2026"):
    rows = summary_rows if summary_rows is not None else [
        _row("Уникальный идентификатор дела", "77RS0030-02-2024-006532-55"),
        _row("Номер дела ~ материала", "02-1234/2026"),
        _row("Cудья", "Тестова Т.Т."),
        _row("Текущее состояние", "Назначено"),
    ]
    tline = timeline_rows if timeline_rows is not None else [
        _tr("Дата", "01.02.2026"),
        _tr("Состояние", "Зарегистрировано"),
        _tr("Документ-основание", ""),
    ]
    hearings = hearings_rows if hearings_rows is not None else []
    return f"""<!doctype html><html><head>
<title>Информация по делу № 02-1234/2026 | Тестовый районный суд</title>
<script>window.reCaptchaApiKey='ysc1_dummy'; require={{paths:{{recaptcha:
"https://smartcaptcha.yandexcloud.net/captcha"}}}};</script></head>
<body><header><p class="drop-title">Тестовый районный суд</p></header><main>
<h1>{h1}</h1>
<div class="anti-container">{''.join(rows)}</div>
<div class="group"><h3>Движение дела</h3><table class="custom_table">
<tbody>{''.join(tline)}</tbody></table></div>
<div class="group"><h3>Судебные заседания</h3><table class="custom_table">
<tbody>{''.join(hearings)}</tbody></table></div>
</main></body></html>"""


def parse_card(html: str) -> mos_parser.MosRecord:
    return parse_mos_html(html, source="mos-gorsud-dom:test",
                          source_url="file://test", retrieved_at=NOW)


class HappyReplayTests(unittest.TestCase):
    def setUp(self):
        self.record = parse_happy()

    def test_summary_fields(self):
        r = self.record
        self.assertEqual(r.case_number, "02-0387/2025")
        self.assertEqual(r.case_uid, "77RS0030-02-2024-006532-55")
        self.assertEqual(r.court, "Хамовнический районный суд")
        self.assertEqual(r.judge, "Перепелкова Т.Г.")
        self.assertEqual(r.status, "Вступило в силу, 08.09.2025")
        self.assertEqual(r.category, "219- Прочие исковые дела")
        self.assertEqual(r.filed_date, "2024-08-19")
        self.assertEqual(r.first_instance_date, "2025-03-28")
        self.assertEqual(r.effective_date, "2025-09-08")
        self.assertEqual(r.related_numbers,
                         ("02-3696/2024", "М-3025/2024"))

    def test_appeal_numbers_and_links(self):
        appeals = {a.number: a.url for a in self.record.appeal_numbers}
        self.assertEqual(
            sorted(appeals), ["33-35100/2025", "33-61969/2025"])
        for url in appeals.values():
            self.assertIn("/mgs/services/cases/appeal-civil/details/", url)

    def test_timeline(self):
        timeline = self.record.timeline
        self.assertEqual(len(timeline), 8)
        self.assertEqual(timeline[0].date, "2024-08-21")
        self.assertEqual(timeline[0].state, "Зарегистрировано")
        self.assertEqual(timeline[-1].date, "2025-11-27")
        self.assertEqual(timeline[-1].state, "Вступило в силу")

    def test_hearings(self):
        hearings = self.record.hearings
        self.assertEqual(len(hearings), 9)
        first = hearings[0]
        self.assertEqual((first.date, first.time, first.hall, first.stage,
                          first.result),
                         ("2024-09-13", "15:00", "4", "Беседа", "Проведена"))
        self.assertTrue(all(h.time and h.date for h in hearings))

    def test_documents_and_links(self):
        docs = self.record.documents
        self.assertEqual(len(docs), 47)
        linked = [d for d in docs if d.link]
        self.assertEqual(len(linked), 2)
        for doc in linked:
            self.assertRegex(doc.link, r"^/rs/hamovnicheskij/cases/docs/"
                                       r"content/[0-9a-f-]{30,}$")
            self.assertTrue(doc.published)
        notes = [d for d in docs if "Готовится к публикации" in d.note]
        self.assertTrue(notes)

    def test_provenance(self):
        import hashlib
        raw = mos_provider().lookup(HAPPY_REF)
        r = parse_mos_html(raw.body, source=raw.source,
                           source_url=raw.source_url,
                           retrieved_at=NOW)
        self.assertEqual(r.source,
                         "mos-gorsud-dom:case-02-0387-2025.redacted.html")
        self.assertIn("m.mos-gorsud.ru", r.source_url)
        self.assertEqual(r.retrieved_at, NOW)
        self.assertEqual(
            r.raw_artifact_hash,
            hashlib.sha256(happy_html().encode("utf-8")).hexdigest())
        snap = r.to_case_snapshot()
        self.assertEqual(snap.provenance.source_url, raw.source_url)
        self.assertEqual(snap.provenance.parser_version,
                         MOS_DOM_PARSER_VERSION)

    def test_canonical_snapshot_mapping(self):
        snap = self.record.to_case_snapshot()
        self.assertEqual(snap.case_number, "02-0387/2025")
        self.assertEqual(snap.court, "Хамовнический районный суд")
        self.assertEqual(snap.status, "Вступило в силу, 08.09.2025")
        self.assertEqual(snap.updated_at, "2025-11-27")
        self.assertIsNone(snap.next_hearing)
        self.assertIsNone(snap.last_event)
        self.assertEqual(snap.parties.plaintiff, ())
        self.assertEqual(snap.parties.respondent, ())
        self.assertEqual(snap.parties.third_parties, ())
        self.assertEqual(len(snap.events_timeline), 8)
        published = [e.name for e in snap.events_timeline if e.published]
        self.assertEqual(published, ["Подготовка к рассмотрению"])
        self.assertEqual(snap.provenance.parser_version,
                         MOS_DOM_PARSER_VERSION)
        self.assertEqual(snap.provenance.retrieved_at, NOW)
        self.assertEqual(len(snap.provenance.raw_artifact_hash), 64)

    def test_parties_never_emitted(self):
        blob = self.record.to_json() + self.record.to_case_snapshot().to_json()
        for token in ("законного представителя", "Истец", "Ответчик",
                      "Стороны", "Долиной"):
            self.assertNotIn(token, blob)
        self.assertTrue(any("PII policy" in w
                            for w in self.record.warnings))

    def test_determinism(self):
        first = parse_happy()
        second = parse_happy()
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.content_hash(), second.content_hash())
        self.assertEqual(first.to_case_snapshot().content_hash(),
                         second.to_case_snapshot().content_hash())


class FailClosedReplayTests(unittest.TestCase):
    def _run_ref(self, case_ref):
        return fetch(mos_provider(), case_ref, NOW)

    def test_waf_is_degraded_never_snapshot(self):
        outcome = self._run_ref("MOS-WAF")
        self.assertEqual(outcome.status, "degraded")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("captcha_or_waf", outcome.reason)

    def test_empty_html_is_error(self):
        outcome = self._run_ref("MOS-EMPTY")
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("empty payload", outcome.reason)

    def test_missing_summary_is_error(self):
        outcome = self._run_ref("MOS-MISSING-SUMMARY")
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("missing required case summary", outcome.reason)

    def test_malformed_timeline_is_error(self):
        outcome = self._run_ref("MOS-MALFORMED")
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("malformed timeline", outcome.reason)

    def test_ambiguous_duplicate_field_is_error(self):
        outcome = self._run_ref("MOS-AMBIGUOUS")
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)
        self.assertIn("ambiguous duplicate summary field", outcome.reason)

    def test_unknown_ref_is_error_not_empty_success(self):
        outcome = self._run_ref("NOPE-9999")
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)

    def test_empty_string_raises_directly(self):
        with self.assertRaises(EmptyPayloadError):
            parse_mos_html("   \n ", source="s", source_url="u",
                           retrieved_at=NOW)

    def test_challenge_html_raises_waf_directly(self):
        html = (MOS_FIXTURES / "waf.html").read_text(encoding="utf-8")
        with self.assertRaises(WafDetectedError):
            parse_mos_html(html, source="s", source_url="u",
                           retrieved_at=NOW)

    def test_captcha_script_text_alone_is_not_waf(self):
        # The healthy card embeds captcha *script paths*; visible-text-only
        # scanning must not false-positive (fixture proves this), but an
        # interstitial with the marker in visible body text must fail.
        with self.assertRaises(WafDetectedError):
            parse_mos_html(
                card_html() + "<div>Подтвердите, что вы не робот</div>",
                source="s", source_url="u", retrieved_at=NOW)
        parse_card(card_html())  # must NOT raise

    def test_title_summary_conflict_is_ambiguous(self):
        with self.assertRaises(MalformedPayloadError) as ctx:
            parse_card(card_html(h1="Информация по делу № 02-7777/2026"))
        self.assertIn("ambiguous case number", str(ctx.exception))

    def test_missing_judge_is_error(self):
        rows = [
            _row("Уникальный идентификатор дела",
                 "77RS0030-02-2024-006532-55"),
            _row("Номер дела ~ материала", "02-1234/2026"),
            _row("Текущее состояние", "Назначено"),
        ]
        with self.assertRaises(MalformedPayloadError) as ctx:
            parse_card(card_html(summary_rows=rows))
        self.assertIn("missing required case summary fields",
                      str(ctx.exception))

    def test_empty_required_field_is_error(self):
        rows = [
            _row("Уникальный идентификатор дела",
                 "77RS0030-02-2024-006532-55"),
            _row("Номер дела ~ материала", "02-1234/2026"),
            _row("Cудья", "   "),
            _row("Текущее состояние", "Назначено"),
        ]
        with self.assertRaises(MalformedPayloadError):
            parse_card(card_html(summary_rows=rows))

    def test_malformed_row_cell_count(self):
        tline = [
            '<tr><td>Дата</td><td>01.02.2026</td><td>extra</td></tr>',
            _tr("Состояние", "Зарегистрировано"),
            _tr("Документ-основание", ""),
        ]
        with self.assertRaises(MalformedPayloadError) as ctx:
            parse_card(card_html(timeline_rows=tline))
        self.assertIn("malformed", str(ctx.exception))

    def test_unterminated_hearing_is_error(self):
        hearings = [
            _tr("Дата", "01.02.2026"), _tr("Время", "09:00"),
            _tr("Зал", "4"), _tr("Стадия", "Судебное заседание"),
            _tr("Результат", "Отложено"), _tr("Основание", ""),
        ]
        with self.assertRaises(MalformedPayloadError) as ctx:
            parse_card(card_html(hearings_rows=hearings))
        self.assertIn("hearing", str(ctx.exception))

    def test_duplicate_hearing_field_is_error(self):
        hearings = [
            _tr("Дата", "01.02.2026"), _tr("Время", "09:00"),
            _tr("Время", "10:00"),
            _tr("Зал", "4"), _tr("Стадия", "Судебное заседание"),
            _tr("Результат", "Отложено"), _tr("Основание", ""),
            _tr("Использовалось аудио/видео протоколирование", ""),
        ]
        with self.assertRaises(MalformedPayloadError) as ctx:
            parse_card(card_html(hearings_rows=hearings))
        self.assertIn("duplicate hearing field", str(ctx.exception))

    def test_invalid_date_is_error(self):
        tline = [
            _tr("Дата", "31.02.2026"),
            _tr("Состояние", "Зарегистрировано"),
            _tr("Документ-основание", ""),
        ]
        with self.assertRaises(MalformedPayloadError):
            parse_card(card_html(timeline_rows=tline))

    def test_no_timeline_section_is_error(self):
        html = (MOS_FIXTURES / "missing-summary.html").read_text(
            encoding="utf-8")
        with self.assertRaises(MalformedPayloadError):
            parse_mos_html(html, source="s", source_url="u",
                           retrieved_at=NOW)


class BoundaryTests(unittest.TestCase):
    def test_json_classifier_refuses_html_payload(self):
        raw = RawSourceResult("s", "u", 200, "<html><body>x</body></html>",
                              media_type="text/html")
        with self.assertRaises(MalformedPayloadError):
            classify(raw)

    def test_fetch_routes_html_provider_to_dom_pipeline(self):
        outcome = fetch(mos_provider(), HAPPY_REF, NOW)
        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(outcome.snapshot)
        self.assertEqual(outcome.snapshot.case_number, "02-0387/2025")

    def test_live_mos_provider_still_quarantined(self):
        with self.assertRaises(SourceUnavailableError) as ctx:
            MosGorsudProvider().lookup("anything")
        self.assertIn("NOT_IMPLEMENTED", str(ctx.exception))
        outcome = fetch(MosGorsudProvider(), HAPPY_REF, NOW)
        self.assertEqual(outcome.status, "error")
        self.assertIsNone(outcome.snapshot)

    def test_classify_html_degraded_status(self):
        raw = RawSourceResult("s", "u", 429, "<html><body>busy</body></html>",
                              media_type="text/html", retry_after=30)
        with self.assertRaises(WafDetectedError):
            classify_html(raw)

    def test_classify_html_http_error(self):
        raw = RawSourceResult("s", "u", 500, "<html><body>oops</body></html>",
                              media_type="text/html")
        from case_search.errors import HttpError
        with self.assertRaises(HttpError):
            classify_html(raw)


class StorageIntegrationTests(unittest.TestCase):
    def test_mos_snapshot_roundtrips_through_store(self):
        outcome = fetch(mos_provider(), HAPPY_REF, NOW)
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            result = watch_case(store, outcome)
            self.assertFalse(result.changed)
            loaded = store.load_latest("02-0387/2025")
            self.assertEqual(loaded.content_hash(),
                             outcome.snapshot.content_hash())
            again = watch_case(store, fetch(mos_provider(), HAPPY_REF, NOW))
            self.assertFalse(again.changed)
            alerts = [
                p for p in Path(tmp).rglob("latest.json")]
            self.assertEqual(len(alerts), 1)


class CliMosReplayTests(unittest.TestCase):
    def _base_args(self, tmp):
        return ["--fixtures", str(MOS_FIXTURES), "--store", f"{tmp}/store",
                "--alerts", f"{tmp}/alerts", "--now", NOW]

    def _run(self, argv):
        from case_search.cli import main
        return main(argv)

    def test_lookup_replay_exit_0_deterministic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv1 = self._base_args(tmp) + [
                "lookup", HAPPY_REF, "--source", "mos-gorsud-dom",
                "--json", f"{tmp}/a.json"]
            argv2 = self._base_args(tmp) + [
                "lookup", HAPPY_REF, "--source", "mos-gorsud-dom",
                "--json", f"{tmp}/b.json"]
            rc1 = self._run(argv1)
            rc2 = self._run(argv2)
            self.assertEqual((rc1, rc2), (0, 0))
            self.assertEqual(Path(f"{tmp}/a.json").read_bytes(),
                             Path(f"{tmp}/b.json").read_bytes())
            self.assertTrue(list(Path(f"{tmp}/store").rglob("latest.json")))

    def test_lookup_waf_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(self._base_args(tmp) + ["lookup", "MOS-WAF",
                                                   "--source",
                                                   "mos-gorsud-dom"])
            self.assertEqual(rc, 2)
            self.assertEqual(len(list(Path(f"{tmp}/alerts").rglob(
                "*.json"))), 1)
            self.assertFalse(list(Path(f"{tmp}/store").rglob(
                "latest.json")))

    def test_lookup_empty_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(self._base_args(tmp) + ["lookup", "MOS-EMPTY",
                                                   "--source",
                                                   "mos-gorsud-dom"])
            self.assertEqual(rc, 3)

    def test_lookup_malformed_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run(self._base_args(tmp) + ["lookup",
                                                   "MOS-MISSING-SUMMARY",
                                                   "--source",
                                                   "mos-gorsud-dom"])
            self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
