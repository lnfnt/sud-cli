# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Command-line interface for sud-cli."""

import argparse
import contextlib
import os
import io
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sud_cli import __version__
from sud_cli.config import get_state_dir
from sud_cli.court_watcher_bridge import run_bridge_command

LIVE_OK_ENV = "SUD_CLI_LIVE_OK"
LIVE_DISABLED_MSG = "live-источники отключены (S2-1..S2-5: egress обходится)"
LIVE_WARNING_MSG = "WARNING: live-источники включены через SUD_CLI_LIVE_OK=1 (S2-1..S2-5: egress обходится)"


def _live_ok() -> bool:
    return os.environ.get(LIVE_OK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
from sud_cli.output import blocked, error, result
from sud_cli.providers import list_providers


def iso_date_arg(raw: str) -> str:
    """Argparse type: строгая дата YYYY-MM-DD (S1-13, --as-of)."""
    import datetime as _dt
    try:
        return _dt.date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ожидается дата YYYY-MM-DD, получено {raw!r}") from exc


def claim_amount_arg(raw: str) -> Decimal:
    """argparse type for ``--claim-amount``: рубли, копейки допустимы (v2.3.1).

    Цена иска — денежная величина, а не счётный объект: п. 1 ст. 333.19,
    ст. 333.21 НК РФ, ст. 91 ГПК РФ, ст. 103 АПК РФ определяют её точной
    суммой с копейками, и округление по п. 6 ст. 52 НК РФ применяется только
    к итоговой пошлине. ``int`` здесь терял копейки на входе, а ``float``
    вносил двоичную погрешность в деньги — поэтому ``Decimal(str)``.

    Zero and negative values are deliberately not rejected here: the rules
    engine already answers them with a typed status, and two different refusal
    paths for one parameter would only invite them to drift apart.
    """
    text = raw.strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(
            f"invalid claim amount {raw!r}: ожидаются рубли, опционально с копейками "
            "(например 3697453 или 3697453.16)"
        ) from None
    if not value.is_finite():
        # Decimal('NaN')/Decimal('Infinity') are valid Decimals and would reach
        # the tariff comparisons as nonsense.
        raise argparse.ArgumentTypeError(
            f"invalid claim amount {raw!r}: цена иска должна быть конечным числом"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sud-cli",
        description="Offline court-document toolkit with a transitional court-watcher bridge.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--state-dir",
        dest="state_dir",
        type=Path,
        default=None,
        help="Application state directory (default: ~/.local/share/sud-cli).",
    )
    parser.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="Emit machine-readable JSON output on stdout.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # providers
    p_providers = subparsers.add_parser(
        "providers", help="List available/offline/live source providers."
    )
    p_providers.set_defaults(func=cmd_providers)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor", help="Check sud-cli's own local state and database."
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_calendar = subparsers.add_parser("calendar", help="Проверка производственного календаря.")
    calendar_sub = p_calendar.add_subparsers(dest="calendar_command", required=True)
    p_calendar_check = calendar_sub.add_parser("check", help="Проверить манифест переносов.")
    p_calendar_check.set_defaults(func=cmd_calendar_check)
    p_calendar_agenda = calendar_sub.add_parser("agenda", help="Единый календарь сроков и слушаний.")
    p_calendar_agenda.add_argument("--days", type=int, default=14)
    p_calendar_agenda.set_defaults(func=cmd_calendar_agenda)

    # ready — agent-facing gate for everything that touches a court host
    p_ready = subparsers.add_parser(
        "ready",
        help=(
            "Report whether live court commands can run (env, binary, license, "
            "Camofox runtime, rules version) and print the calling conventions. "
            "Exits non-zero when something is missing. Contacts no court host."
        ),
    )
    p_ready.set_defaults(func=cmd_ready)

    # import
    p_import = subparsers.add_parser(
        "import", help="Import a local JSON fixture into the offline store."
    )
    p_import.add_argument("kind", choices=["court", "npa"], help="Record kind to import.")
    p_import.add_argument("file", type=Path, help="Path to a local JSON file.")
    p_import.set_defaults(func=cmd_import)

    # search
    p_search = subparsers.add_parser(
        "search", help="Search imported offline records."
    )
    p_search.add_argument("kind", choices=["court", "npa"], help="Record kind to search.")
    p_search.add_argument("query", nargs="?", default=None, help="Free-text query.")
    p_search.add_argument("--limit", type=int, default=20, help="Maximum results (default: 20).")
    p_search.add_argument("--since", type=str, default=None, help="Start date (YYYY-MM-DD).")
    p_search.add_argument("--until", type=str, default=None, help="End date (YYYY-MM-DD).")
    p_search.set_defaults(func=cmd_search)

    # npa
    p_npa = subparsers.add_parser(
        "npa", help="NPA fixture search and edition comparison."
    )
    npa_sub = p_npa.add_subparsers(dest="npa_command", required=True)
    npa_search = npa_sub.add_parser("search", help="Search NPA fixtures.")
    npa_search.add_argument("query", nargs="?", default=None)
    npa_search.add_argument("--limit", type=int, default=20)
    npa_search.set_defaults(func=cmd_npa_search)
    npa_compare = npa_sub.add_parser("compare", help="Compare two NPA fixture editions.")
    npa_compare.add_argument("--id", dest="doc_id", required=True)
    npa_compare.add_argument("--edition1", required=True)
    npa_compare.add_argument("--edition2", required=True)
    npa_compare.set_defaults(func=cmd_npa_compare)
    npa_download = npa_sub.add_parser(
        "download", help="Download a full official NPA/court-act text into local storage."
    )
    npa_download.add_argument(
        "--eo-number", dest="eo_number", type=str, default=None,
        help="pravo.gov.ru publication number (e.g. 0001202509240023).",
    )
    npa_download.add_argument(
        "--url", type=str, default=None,
        help="Direct official URL (pravo.gov.ru / vsrf.ru).",
    )
    npa_download.add_argument(
        "--source", choices=["pravo", "vsrf", "url"], default="pravo",
        help="Source kind (default: pravo with --eo-number).",
    )
    npa_download.add_argument(
        "--case-num", dest="case_num", type=str, default=None,
        help="Case number (source=vsrf; requires a direct URL).",
    )
    npa_download.add_argument(
        "--out-dir", dest="out_dir", type=Path, default=None,
        help="Storage directory (default: STATE_DIR/npa).",
    )
    npa_download.add_argument(
        "--extract-text", dest="extract_text",
        action=argparse.BooleanOptionalAction, default=True,
        help="Extract the PDF text layer into a sidecar .txt (default: yes).",
    )
    npa_download.set_defaults(func=cmd_npa_download)

    # law — official publication metadata search
    p_law = subparsers.add_parser(
        "law", help="Official legal publication metadata search."
    )
    law_sub = p_law.add_subparsers(dest="law_command", required=True)
    p_law_search = law_sub.add_parser(
        "search", help="Search official publication metadata (one HTTPS GET)."
    )
    p_law_search.add_argument(
        "query", nargs="?", default=None, help="Free-text metadata query."
    )
    p_law_search.add_argument(
        "--number", type=str, default=None, help="Exact document number."
    )
    p_law_search.set_defaults(func=cmd_law_search)

    # court — native offline case/event timeline
    p_court = subparsers.add_parser(
        "court", help="Native offline court case import and timeline."
    )
    court_sub = p_court.add_subparsers(dest="court_command", required=True)

    p_court_import = court_sub.add_parser(
        "import", help="Import a local synthetic court case/event fixture."
    )
    p_court_import.add_argument("file", type=Path, help="Path to a local JSON file.")
    p_court_import.set_defaults(func=cmd_court_import)

    p_court_search = court_sub.add_parser(
        "search", help="Search imported native court cases and events."
    )
    p_court_search.add_argument("query", nargs="?", default=None, help="Free-text query.")
    p_court_search.add_argument(
        "--limit", type=int, default=20, help="Maximum results (default: 20)."
    )
    p_court_search.add_argument("--since", type=str, default=None, help="Start date (YYYY-MM-DD).")
    p_court_search.add_argument("--until", type=str, default=None, help="End date (YYYY-MM-DD).")
    p_court_search.set_defaults(func=cmd_court_search)

    p_court_case = court_sub.add_parser(
        "case", help="Show a normalized case summary and event timeline."
    )
    p_court_case.add_argument("case_id", help="Synthetic case identifier.")
    p_court_case.set_defaults(func=cmd_court_case)

    # case-search — safe transitional compatibility bridge.
    # Публичное имя case-search (ребрендинг 02.11); court-watcher оставлен
    # алиасом: команда мостит к внешнему бинарю ``court-watcher``, чей
    # контракт (имя на диске, протокол) менять нельзя.
    p_cw = subparsers.add_parser(
        "case-search",
        aliases=["court-watcher"],
        help="Transitional compatibility bridge to the court-watcher CLI.",
    )
    cw_sub = p_cw.add_subparsers(dest="cw_command", required=True)

    p_cw_providers = cw_sub.add_parser(
        "providers", help="Bridge to court-watcher provider status."
    )
    p_cw_providers.set_defaults(func=cmd_court_watcher_providers)

    for cw_cmd in ("import", "sudrf-import", "kad-import", "vsrf-import", "auto-import"):
        p_cw_import = cw_sub.add_parser(
            cw_cmd,
            help=f"Bridge to court-watcher {cw_cmd} (saved file -> JSON).",
        )
        p_cw_import.add_argument("file", type=Path, help="Path to a saved file under the sud-cli state directory.")
        p_cw_import.add_argument("--case", required=True, dest="case_id", help="Synthetic case identifier.")
        p_cw_import.set_defaults(func=cmd_court_watcher_import)

    p_cw_extract = cw_sub.add_parser(
        "extract-text", help="Bridge to court-watcher extract-text (saved file -> text)."
    )
    p_cw_extract.add_argument("file", type=Path, help="Path to a saved file under the sud-cli state directory.")
    p_cw_extract.set_defaults(func=cmd_court_watcher_extract)

    p_cw_parity = cw_sub.add_parser(
        "parity", help="Show sud-cli vs. court-watcher capability parity matrix."
    )
    p_cw_parity.set_defaults(func=cmd_court_watcher_parity)

    p_cw_diff = cw_sub.add_parser(
        "case-diff",
        help="Compare two saved case snapshots (offline, no court contact).",
    )
    p_cw_diff.add_argument("old", type=Path, help="Earlier snapshot JSON.")
    p_cw_diff.add_argument("new", type=Path, help="Later snapshot JSON.")
    p_cw_diff.set_defaults(func=cmd_case_diff)

    # Live flag-driven commands (Phase 8). These reach external court sources
    # and stay gated behind SUD_CLI_BRIDGE_LIVE inside the bridge.
    p_cw_search = cw_sub.add_parser(
        "search-practice",
        help="Live sudact practice search (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_search.add_argument("query", help="Search phrase.")
    p_cw_search.add_argument("--section", choices=["arbitral", "regular", "magistrate", "vsrf"], default=None)
    p_cw_search.add_argument("--page", default=None, help="Result page (1..1000).")
    p_cw_search.add_argument("--limit", default=None, help="Max results (1..30).")
    p_cw_search.add_argument("--via-camofox", action="store_true", dest="via_camofox",
                             help="Use the external Camofox runtime instead of the embedded browser.")
    p_cw_search.add_argument("--no-camofox", action="store_true", dest="no_camofox",
                             help="Force the embedded browser even when Camofox is running.")
    p_cw_search.set_defaults(func=cmd_court_watcher_search_practice, via_camofox=True,
                             no_camofox=False)

    p_cw_kad = cw_sub.add_parser(
        "kad-search",
        help="Live KAD participant search (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_kad.add_argument("--inn", default=None, help="Participant INN (10 or 12 digits).")
    p_cw_kad.add_argument("--ogrn", default=None, help="Participant OGRN/OGRNIP (13 or 15 digits).")
    p_cw_kad.add_argument("--name", default=None, help="Participant name.")
    p_cw_kad.add_argument("--page", default=None, help="Result page (1..1000).")
    p_cw_kad.set_defaults(func=cmd_court_watcher_kad_search)

    p_cw_gettext = cw_sub.add_parser(
        "get-text",
        help="Live sudact.ru document text (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_gettext.add_argument("url", help="sudact.ru document URL.")
    p_cw_gettext.add_argument("--no-cache", action="store_true", dest="no_cache",
                              help="Force refetch, ignore the upstream SQLite cache.")
    p_cw_gettext.add_argument("--blocks", action="store_true",
                              help="Return УСТАНОВИЛ/РЕШИЛ blocks separately.")
    p_cw_gettext.set_defaults(func=cmd_court_watcher_get_text)

    # Positional live commands (Phase 11). These four were already vetted in the
    # bridge allowlist but had no CLI entry point, so the bridge could reach
    # them while the user could not.
    p_cw_lookup = cw_sub.add_parser(
        "lookup",
        help="Live case lookup by case id (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_lookup.add_argument("case_id", help="Case id, e.g. А40-12345/2026 or А40-12345/25.")
    p_cw_lookup.add_argument(
        "--source",
        choices=["kad", "mos-gorsud", "mos-gorsud-dom", "sudrf"],
        default=None,
        help=(
            "Upstream data source. Without it court-watcher defaults to its "
            "'synthetic' provider, which only knows fixture ids, so a real "
            "case reference always fails — pass a real source."
        ),
    )
    p_cw_lookup.set_defaults(func=cmd_court_watcher_lookup)

    p_cw_hist = cw_sub.add_parser(
        "case-history",
        help="Live case hearing history (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_hist.add_argument("case_id", help="Case id, e.g. А40-12345/2026.")
    p_cw_hist.set_defaults(func=cmd_court_watcher_case_history)

    p_cw_sudrf = cw_sub.add_parser(
        "sudrf-live",
        help="Live ГАС «Правосудие» lookup (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_sudrf.add_argument("case_id", help="Case id, e.g. А40-12345/2026.")
    p_cw_sudrf.set_defaults(func=cmd_court_watcher_sudrf_live)

    p_cw_mos = cw_sub.add_parser(
        "mos-live",
        help="Live mos.ru case lookup (needs SUD_CLI_BRIDGE_LIVE=1).",
    )
    p_cw_mos.add_argument("court_slug", help="mos.ru court slug.")
    p_cw_mos.add_argument("case_id", help="Case id, e.g. А40-12345/2026.")
    p_cw_mos.set_defaults(func=cmd_court_watcher_mos_live)

    # First-class superapp aliases. These call the embedded Python API directly;
    # the court-watcher namespace above remains the compatibility bridge.
    p_practice = subparsers.add_parser("practice", help="Search judicial practice.")
    p_practice.add_argument("query")
    p_practice.add_argument("--section", choices=["arbitral", "regular", "magistrate", "vsrf"], default="arbitral")
    p_practice.add_argument("--page", type=int, default=1)
    p_practice.add_argument("--limit", type=int, default=10)
    p_practice.set_defaults(func=cmd_native_practice)

    p_kad = subparsers.add_parser("kad", help="Search KAD by participant.")
    p_kad.add_argument("--inn")
    p_kad.add_argument("--ogrn")
    p_kad.add_argument("--name")
    p_kad.add_argument("--page", type=int, default=1)
    p_kad.set_defaults(func=cmd_native_kad)

    p_history = subparsers.add_parser("case-history", help="Show case history.")
    p_history.add_argument("case_id")
    p_history.add_argument("--section", choices=["arbitral", "regular", "magistrate", "vsrf"], default="arbitral")
    p_history.add_argument("--max-related", type=int, default=5)
    p_history.add_argument("--limit", type=int, default=10)
    p_history.set_defaults(func=cmd_native_case_history)

    p_sudrf = subparsers.add_parser("sudrf-live", help="Look up a ГАС Правосудие case.")
    p_sudrf.add_argument("case_id")
    p_sudrf.add_argument("--host", default="oblsud.mo.sudrf.ru")
    p_sudrf.set_defaults(func=cmd_native_sudrf)

    p_mos = subparsers.add_parser("mos-live", help="Look up a Moscow court case.")
    p_mos.add_argument("court_slug")
    p_mos.add_argument("case_id")
    p_mos.set_defaults(func=cmd_native_mos)

    # v0.1.0 ЕФРСБ (bankrot.fedresurs.ru) — банкротные проверки (мандат владельца 20.10)
    p_efrsb = subparsers.add_parser(
        "efrsb", help="ЕФРСБ: банкротные проверки ЮЛ/ИП/физлиц, публикации дел, управляющие.")
    efrsb_sub = p_efrsb.add_subparsers(dest="efrsb_cmd", required=True)
    e_companies = efrsb_sub.add_parser("companies", help="Поиск ЮЛ/ИП в банкротстве.")
    e_companies.add_argument("--inn")
    e_companies.add_argument("--ogrn")
    e_companies.add_argument("--name")
    e_persons = efrsb_sub.add_parser("persons", help="Поиск физлиц в банкротстве.")
    e_persons.add_argument("--name", required=True)
    e_persons.add_argument("--birth-date", default=None)
    e_pubs = efrsb_sub.add_parser("publications", help="Публикации по делу.")
    e_pubs.add_argument("--case-id", required=True)
    e_managers = efrsb_sub.add_parser("managers", help="Арбитражные управляющие.")
    e_managers.add_argument("--search")
    e_managers.add_argument("--inn")
    e_managers.add_argument("--sro")
    for e_sub in (e_companies, e_persons, e_pubs, e_managers):
        e_sub.add_argument("--limit", type=int, default=5)
        e_sub.set_defaults(func=cmd_efrsb)

    p_text = subparsers.add_parser("extract-text", help="Extract act text.")
    p_text.add_argument("file", type=Path)
    p_text.set_defaults(func=cmd_native_text)

    p_card = subparsers.add_parser("import-card", help="Import a saved case card.")
    p_card.add_argument("file", type=Path)
    p_card.add_argument("--case", required=True, dest="case_id")
    p_card.set_defaults(func=cmd_native_import_card)

    # fees — state fee calculation (approved spec v1.0.0, ФЗ-259 от 08.08.2024)
    p_fees = subparsers.add_parser(
        "fees",
        help="Расчёт госпошлины (справка, ст. 333.19/333.21 НК РФ, ред. 259-ФЗ).",
    )
    p_fees.add_argument(
        "--jurisdiction",
        required=True,
        choices=["arbitration", "general_jurisdiction", "kas"],
        help=(
            "AS — арбитражные суды (АПК РФ); SOYU — суды общей юрисдикции (ГПК РФ); "
            "KAS — административное судопроизводство (КАС РФ, тарифы ст. 333.19 НК РФ)"
        ),
    )
    p_fees.add_argument(
        "--kind",
        required=True,
        choices=[
            "property_claim",
            "court_order",
            "non_property_claim",
            "appeal",
            "cassation_first",
            "cassation_supreme",
            "bankruptcy_petition",
            "bankruptcy_creditor_claim",
        ],
        help="Вид требования/обжалования",
    )
    p_fees.add_argument(
        "--claim-amount",
        type=claim_amount_arg,
        default=None,
        help=(
            "Цена иска в рублях, копейки допускаются и не отбрасываются "
            "(3697453.16); нужна для имущественных исков и судебного приказа"
        ),
    )
    p_fees.add_argument(
        "--applicant",
        choices=["individual", "entity"],
        default=None,
        help="Заявитель: физлицо или организация (нужен для фиксированных ставок)",
    )
    p_fees.add_argument(
        "--claim-class",
        default=None,
        help="Класс дела; bankruptcy/criminal/administrative_offence отклоняются",
    )
    p_fees.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        type=iso_date_arg,
        help=(
            "Дата подачи заявления YYYY-MM-DD: тарифы берутся из редакции НК РФ, "
            "действовавшей на эту дату (S1-13; до 2024-09-08 — ред. 221-ФЗ)"
        ),
    )
    p_fees.set_defaults(func=cmd_fees)

    # deadlines — procedural deadline calculation (approved spec v1.0.0)
    p_deadlines = subparsers.add_parser(
        "deadlines",
        help="Расчёт процессуального срока (АПК РФ / ГПК РФ).",
    )
    deadline_sub = p_deadlines.add_subparsers(dest="deadlines_command")
    p_deadlines.add_argument(
        "deadline_key",
        nargs="?",
        default=None,
        help=(
            "Ключ срока (legacy). АПК: as_appeal_decision, as_appeal_simplified, as_appeal_ruling, "
            "as_cassation_district, as_court_order_cancel, as_court_order_cassation. "
            "ГПК: soyu_appeal_decision, soyu_appeal_ruling, soyu_cassation, "
            "soyu_court_order_cancel, soyu_court_order_cassation. "
            "КАС: kas_appeal_decision, kas_cassation, kas_court_order_cancel"
        ),
    )
    p_deadlines.add_argument(
        "--event-date",
        default=None,
        help="Дата события (Day 0) в формате ГГГГ-ММ-ДД",
    )
    p_deadlines.add_argument("--region", default=None, help="Код субъекта РФ, например 16.")
    p_deadlines.add_argument("--tz", "--timezone", dest="tz", default="Europe/Moscow", help="IANA timezone суда.")
    p_deadlines.add_argument("--case", "--case-id", dest="case_id", default=None, help="Привязать расчёт к делу.")
    p_deadlines.add_argument("--confirm", action="store_true", help="Сразу подтвердить срок (anti-laches).")
    p_deadlines.add_argument("--by", default=None, help="Актор подтверждения/anchor.")
    p_deadlines.add_argument("--evidence", default=None, help="Доказательство (вручение и т.п.).")
    p_deadlines.add_argument("--norm", default=None, help="Норма-ссылка для waiver.")
    p_deadlines.add_argument("--waive", action="store_true", help="Зафиксировать waiver (требует --norm).")
    p_deadlines.set_defaults(func=cmd_deadlines)

    p_dc = deadline_sub.add_parser("calculate", aliases=["calc"], help="Рассчитать срок и (опц.) привязать к делу.")
    p_dc.add_argument("calc_case_id", nargs="?", default=None, help="ID дела (опционально).")
    p_dc.add_argument("calc_deadline_key", help="Ключ срока.")
    p_dc.add_argument("--event-date", dest="event_date", default=None)
    p_dc.add_argument("--region", default=None)
    p_dc.add_argument("--tz", "--timezone", dest="tz", default="Europe/Moscow")
    p_dc.add_argument("--case", "--case-id", dest="case_id", default=None)
    p_dc.add_argument("--confirm", action="store_true")
    p_dc.add_argument("--by", default=None)
    p_dc.add_argument("--evidence", default=None)
    p_dc.set_defaults(func=cmd_deadlines_calculate)

    p_dconf = deadline_sub.add_parser("confirm", help="Подтвердить срок.")
    p_dconf.add_argument("case_id")
    p_dconf.add_argument("--key", required=True, dest="key")
    p_dconf.add_argument("--by", required=True)
    p_dconf.add_argument("--evidence", default=None)
    p_dconf.add_argument("--waive", action="store_true")
    p_dconf.add_argument("--norm", default=None)
    p_dconf.set_defaults(func=cmd_deadlines_confirm)

    p_danchor = deadline_sub.add_parser("anchor", help="Зафиксировать начало срока с доказательством.")
    p_danchor.add_argument("case_id")
    p_danchor.add_argument("--key", required=True, dest="key")
    p_danchor.add_argument("--event-date", dest="event_date", required=True)
    p_danchor.add_argument("--method", required=True, choices=["вручение", "публикация", "почта", "иное"])
    p_danchor.add_argument("--evidence", required=True)
    p_danchor.add_argument("--by", default=None)
    p_danchor.set_defaults(func=cmd_deadlines_anchor)

    p_dwaive = deadline_sub.add_parser("waive", help="Waiver срока с нормой-ссылкой.")
    p_dwaive.add_argument("case_id")
    p_dwaive.add_argument("--key", required=True, dest="key")
    p_dwaive.add_argument("--by", required=True)
    p_dwaive.add_argument("--norm", required=True)
    p_dwaive.add_argument("--evidence", default=None)
    p_dwaive.set_defaults(func=cmd_deadlines_waive)

    p_daudit = deadline_sub.add_parser("audit", help="Журнал правок сроков.")
    p_daudit.add_argument("case_id")
    p_daudit.set_defaults(func=cmd_deadlines_audit)

    p_dlist = deadline_sub.add_parser("list", help="Список состояний сроков.")
    p_dlist.add_argument("case_id", nargs="?", default=None)
    p_dlist.add_argument("--case", "--case-id", dest="case_id", default=None)
    p_dlist.set_defaults(func=cmd_deadlines_list)

    p_dmatrix = deadline_sub.add_parser("matrix", help="Матрица контрольных точек дела.")
    p_dmatrix.add_argument("case_id", nargs="?", default=None)
    p_dmatrix.add_argument("--case", "--case-id", dest="case_id", default=None)
    p_dmatrix.add_argument("--as-of", dest="as_of", default=None, help="Дата as-of (не влияет на расчёт).")
    p_dmatrix.add_argument("--instance", choices=["apk", "gpk", "kas"], default=None, help="Override инстанции дела.")
    p_dmatrix.add_argument("--missing", action="store_true", help="Только точки с needs_anchor.")
    p_dmatrix.set_defaults(func=cmd_deadlines_matrix)

    p_interest = subparsers.add_parser("interest", help="Проценты по ст. 395 или 317.1 ГК РФ.")
    p_interest.add_argument("--amount", "--principal", dest="amount", type=int, required=True)
    p_interest.add_argument("--from", "--start", dest="start_date", required=True)
    p_interest.add_argument("--to", "--end", dest="end_date", required=True)
    p_interest.add_argument("--article", choices=["395", "317.1"], default="395")
    p_interest.add_argument("--calc-fee", action="store_true")
    p_interest.add_argument("--jurisdiction", choices=["arbitration", "general_jurisdiction", "kas"], default="arbitration")
    p_interest.add_argument("--kind", default="property_claim")
    p_interest.add_argument("--applicant", choices=["individual", "entity"], default="entity")
    p_interest.add_argument("--payments", default=None, help="Частичные оплаты: YYYY-MM-DD:сумма,... или JSON-список")
    p_interest.set_defaults(func=cmd_interest)

    p_payment = subparsers.add_parser("payment", help="Реквизиты и назначение платежа.")
    p_payment.add_argument("--jurisdiction", choices=["arbitration", "general_jurisdiction", "kas", "soyu", "eks"], required=True)
    p_payment.add_argument("--amount", type=str, required=True)
    p_payment.add_argument("--court", required=True)
    p_payment.add_argument("--defendant", required=True)
    p_payment.set_defaults(func=cmd_payment)

    p_deferral = subparsers.add_parser("deferral", help="Ходатайство об отсрочке госпошлины.")
    p_deferral.add_argument("--applicant", "--claimant", dest="applicant", required=True)
    p_deferral.add_argument("--court", required=True)
    p_deferral.add_argument("--amount", type=int, required=True)
    p_deferral.add_argument("--reason", "--grounds", dest="reason", required=True)
    p_deferral.add_argument("--court-type", dest="court_type", default="arbitration",
                            choices=["arbitration", "general"],
                            help="Тип суда (W6/S1-11): arbitration — АПК+Пленум ВАС №6; "
                                 "general — ГПК/КАС без АПК и ВАС")
    p_deferral.set_defaults(func=cmd_deferral)

    p_feeex = subparsers.add_parser("fee-exemption", help="Льгота по госпошлине (VERIFIED-выборка категорий ст.333.35-37).")
    p_feeex.add_argument("--jurisdiction", required=True, choices=["apk", "gpk", "kas", "arbitration", "general_jurisdiction"])
    p_feeex.add_argument("--category", required=True)
    p_feeex.set_defaults(func=cmd_fee_exemption)

    p_feeref = subparsers.add_parser("fee-refund", help="Возврат госпошлины по основаниям ст.333.40 НК РФ (VERIFIED).")
    p_feeref.add_argument("--paid", required=True, help="Уплаченная сумма, Decimal")
    p_feeref.add_argument("--ground", required=True)
    p_feeref.add_argument("--claim-admitted", action="store_true")
    p_feeref.add_argument("--plaintiff-default", action="store_true")
    p_feeref.add_argument("--transferred-to-arbitration", action="store_true")
    p_feeref.set_defaults(func=cmd_fee_refund)

    p_feeidx = subparsers.add_parser("fee-indexation", help="Рамка/расчёт индексации 183 АПК / 208 ГПК / 189.1 КАС.")
    p_feeidx.add_argument("--code", required=True, choices=["apk", "gpk", "kas"])
    p_feeidx.add_argument("--amount")
    p_feeidx.add_argument("--cpi-start")
    p_feeidx.add_argument("--cpi-end")
    p_feeidx.set_defaults(func=cmd_fee_indexation)

    p_limitation = subparsers.add_parser("limitation", aliases=["laches"], help="Расчёт срока исковой давности.")
    p_limitation.add_argument("--start", required=True)
    p_limitation.add_argument("--type", dest="period_type", default="general",
                              choices=["general", "contract_work", "carriage", "voidable_deal", "custom_years", "custom_months"])
    p_limitation.add_argument("--years", type=int, default=0)
    p_limitation.add_argument("--months", type=int, default=0)
    p_limitation.add_argument("--claim-notice-days", type=int, default=0)
    p_limitation.add_argument("--pretrial-start", dest="pretrial_start", default=None,
                              type=iso_date_arg,
                              help="Дата начала досудебного порядка YYYY-MM-DD "
                                   "(W6/S1-3: приостановление срока, п. 3 ст. 202 ГК РФ)")
    p_limitation.add_argument("--pretrial-end", dest="pretrial_end", default=None,
                              type=iso_date_arg,
                              help="Дата окончания досудебного порядка YYYY-MM-DD "
                                   "(передаётся только вместе с --pretrial-start)")
    p_limitation.add_argument("--check-date", default=None)
    p_limitation.set_defaults(func=cmd_limitation)

    p_audit = subparsers.add_parser("timeline-audit", help="Проверить процессуальную хронологию дела.")
    p_audit.add_argument("--events", required=True, help="JSON-файл или сырой JSON-массив событий.")
    p_audit.set_defaults(func=cmd_timeline_audit)

    p_asof = subparsers.add_parser("npa-as-of", help="Получить редакцию нормы на дату события.")
    p_asof.add_argument("--act", required=True)
    p_asof.add_argument("--article", required=True)
    p_asof.add_argument("--date", required=True)
    p_asof.set_defaults(func=cmd_npa_as_of)

    # verify — Закон Zero: валидация цитат (v2.9.0, S-52)
    p_verify = subparsers.add_parser(
        "verify", help="Verify a quoted fragment against an explicit local source."
    )
    verify_sub = p_verify.add_subparsers(dest="verify_command", required=True)
    p_vq = verify_sub.add_parser("quote", help="Check a quotation (npa bundle | case events | text file).")
    p_vq.add_argument("--text", required=True)
    p_vq.add_argument("--source", required=True, choices=["npa", "case", "file"])
    p_vq.add_argument("--act")
    p_vq.add_argument("--article")
    p_vq.add_argument("--date")
    p_vq.add_argument("--case-id", dest="case_id")
    p_vq.add_argument("--path")
    p_vq.add_argument("--max-fuzzy", dest="max_fuzzy", type=float, default=0.0)
    p_vq.set_defaults(func=cmd_verify_quote)

    # practice-status — offline fate probe (v2.9.0, S-54). The user-facing form
    # is `practice status <n>`: main() rewrites it here because the live
    # `practice <query>` parser must stay flat for the MCP drift contract.
    p_ps = subparsers.add_parser(
        "practice-status", help="Fate probe: possibly_overturned|unknown из ЛОКАЛЬНЫХ артефактов."
    )
    p_ps.add_argument("case_number")
    p_ps.add_argument("--related", default=None, help="Fixture-режим: заголовки related-инстансов через ';'.")
    p_ps.add_argument("--store", type=Path, default=None, help="Каталог snapshot/diff артефактов court-watcher.")
    p_ps.set_defaults(func=cmd_practice_status)


    p_mcp = subparsers.add_parser("serve-mcp", help="Запустить локальный MCP JSON-RPC stdio сервер.")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    # case — central case registry (v2.4.0)
    p_case = subparsers.add_parser("case", help="Реестр дел.")
    case_sub = p_case.add_subparsers(dest="case_command", required=True)

    p_case_new = case_sub.add_parser("new", help="Создать дело.")
    p_case_new.add_argument("--number", required=True, dest="number")
    p_case_new.add_argument("--court", required=True)
    p_case_new.add_argument("--plaintiff", required=True)
    p_case_new.add_argument("--defendant", required=True)
    p_case_new.add_argument("--owner", default=None)
    p_case_new.add_argument("--status", default=None)
    p_case_new.set_defaults(func=cmd_case_new)

    p_case_set = case_sub.add_parser("set", help="Обновить дело.")
    p_case_set.add_argument("case_id")
    p_case_set.add_argument("--status", default=None)
    p_case_set.add_argument("--owner", default=None)
    p_case_set.add_argument("--closed-at", dest="closed_at", default=None)
    p_case_set.set_defaults(func=cmd_case_set)

    p_case_amount = case_sub.add_parser("amount", help="Суммы по делу.")
    amount_sub = p_case_amount.add_subparsers(dest="case_amount_command", required=True)
    p_amount_add = amount_sub.add_parser("add", help="Добавить сумму.")
    p_amount_add.add_argument("case_id")
    p_amount_add.add_argument("--kind", required=True)
    p_amount_add.add_argument("--amount", required=True)
    p_amount_add.add_argument("--note", default=None)
    p_amount_add.set_defaults(func=cmd_case_amount_add)
    p_amount_list = amount_sub.add_parser("list", help="Список сумм.")
    p_amount_list.add_argument("case_id")
    p_amount_list.set_defaults(func=cmd_case_amount_list)
    p_amount_set = amount_sub.add_parser("set", help="Статус/дата платежа по сумме (W4b).")
    p_amount_set.add_argument("case_id")
    p_amount_set.add_argument("amount_id", type=int)
    p_amount_set.add_argument("--status", required=True,
                              choices=["planned", "paid", "refunded"])
    p_amount_set.add_argument("--paid-at", dest="paid_at", default=None,
                              help="Дата платежа YYYY-MM-DD")
    p_amount_set.set_defaults(func=cmd_case_amount_set)
    p_amount_summary = amount_sub.add_parser("summary", help="Сводка сумм по kind x status (W4b).")
    p_amount_summary.add_argument("case_id")
    p_amount_summary.set_defaults(func=cmd_case_amount_summary)

    p_case_doc = case_sub.add_parser("doc", help="Документы по делу.")
    doc_sub = p_case_doc.add_subparsers(dest="case_doc_command", required=True)
    p_doc_add = doc_sub.add_parser("add", help="Добавить документ.")
    p_doc_add.add_argument("case_id")
    p_doc_add.add_argument("--path", required=True)
    p_doc_add.add_argument("--title", default=None)
    p_doc_add.set_defaults(func=cmd_case_doc_add)
    p_doc_list = doc_sub.add_parser("list", help="Список документов.")
    p_doc_list.add_argument("case_id")
    p_doc_list.set_defaults(func=cmd_case_doc_list)

    p_case_note = case_sub.add_parser("note", help="Заметки по делу.")
    note_sub = p_case_note.add_subparsers(dest="case_note_command", required=True)
    p_note_add = note_sub.add_parser("add", help="Добавить заметку.")
    p_note_add.add_argument("case_id")
    p_note_add.add_argument("--text", required=True)
    p_note_add.add_argument("--author", default=None)
    p_note_add.set_defaults(func=cmd_case_note_add)
    p_note_list = note_sub.add_parser("list", help="Список заметок.")
    p_note_list.add_argument("case_id")
    p_note_list.set_defaults(func=cmd_case_note_list)
    p_note_sup = note_sub.add_parser("supersede", help="Заменить заметку.")
    p_note_sup.add_argument("note_id")
    p_note_sup.add_argument("--text", required=True)
    p_note_sup.add_argument("--author", default=None)
    p_note_sup.set_defaults(func=cmd_case_note_supersede)

    p_case_list = case_sub.add_parser("list", help="Список дел.")
    p_case_list.add_argument("--status", default=None)
    p_case_list.add_argument("--owner", default=None)
    p_case_list.add_argument("--court", default=None)
    p_case_list.add_argument("--party", default=None)
    p_case_list.set_defaults(func=cmd_case_list)

    p_case_card = case_sub.add_parser("card", help="Карточка дела.")
    p_case_card.add_argument("case_id")
    p_case_card.set_defaults(func=cmd_case_card)

    # reminders — bus-free генератор напоминаний (W4b §2, S-26)
    p_reminders = subparsers.add_parser("reminders", help="Напоминания по срокам (bus-free).")
    reminders_sub = p_reminders.add_subparsers(dest="reminders_command", required=True)
    p_rem_due = reminders_sub.add_parser(
        "due", help="Сроки в окне дней + готовые a2a-строки; sud шину не вызывает.")
    p_rem_due.add_argument("--days", type=int, default=7, help="Окно выборки, 1..90")
    p_rem_due.set_defaults(func=cmd_reminders_due)

    # conflict — conflict-of-interest heuristic (v2.4.1)
    p_conflict = subparsers.add_parser("conflict", help="Детект конфликта интересов (name-exact).")
    conflict_sub = p_conflict.add_subparsers(dest="conflict_command", required=True)

    p_conflict_check = conflict_sub.add_parser("check", help="Проверка кандидата по локальному реестру.")
    p_conflict_check.add_argument("--plaintiff", default=None)
    p_conflict_check.add_argument("--defendant", default=None)
    p_conflict_check.add_argument("--court", default=None)
    p_conflict_check.add_argument("--case-number", dest="case_number", default=None)
    p_conflict_check.set_defaults(func=cmd_conflict_check)

    p_conflict_case = conflict_sub.add_parser("case", help="Проверка существующего дела.")
    p_conflict_case.add_argument("case_id")
    p_conflict_case.set_defaults(func=cmd_conflict_case)

    # jurisdiction — подсудность на VERIFIED rules (v2.6.0)
    p_jur = subparsers.add_parser("jurisdiction", help="Гейдер подсудности по VERIFIED правилам.")
    jur_sub = p_jur.add_subparsers(dest="jurisdiction_command", required=True)
    p_jur_check = jur_sub.add_parser("check", help="Проверка подсудности.")
    p_jur_check.add_argument("--code", required=True, choices=["apk", "gpk", "kas"])
    p_jur_check.add_argument("--claim", required=True)
    p_jur_check.add_argument("--party-org", dest="party_org", action="store_true")
    p_jur_check.add_argument("--party-phys", dest="party_phys", action="store_true")
    p_jur_check.add_argument("--unknown-rf-location", dest="unknown_rf_location", action="store_true")
    p_jur_check.add_argument("--immovable", action="store_true")
    p_jur_check.add_argument("--corporate", action="store_true")
    p_jur_check.add_argument("--bankruptcy", action="store_true")
    p_jur_check.add_argument("--ip-rights", dest="ip_rights", action="store_true")
    p_jur_check.add_argument("--contract-forum", dest="contract_forum", action="store_true")
    p_jur_check.add_argument("--arbitration-clause", dest="arbitration_clause", action="store_true")
    p_jur_check.add_argument("--multiple-defendants", dest="multiple_defendants", action="store_true")
    p_jur_check.add_argument("--foreign-element", dest="foreign_element", action="store_true")
    p_jur_check.add_argument("--consumer", action="store_true")
    p_jur_check.add_argument("--alimony", action="store_true")
    p_jur_check.set_defaults(func=cmd_jurisdiction_check)

    # pretrial — претензионный порядок (v2.6.0)
    p_pre = subparsers.add_parser("pretrial", help="Претензионный порядок по VERIFIED правилам.")
    pre_sub = p_pre.add_subparsers(dest="pretrial_command", required=True)
    p_pre_calc = pre_sub.add_parser("calc", help="Срок ответа на претензию.")
    p_pre_calc.add_argument("--code", required=True, choices=["apk", "zozpp", "osago",
                                                              "railway", "road", "maritime",
                                                              "inland", "air", "freight", "postal"])
    p_pre_calc.add_argument("--sent", required=True)
    p_pre_calc.set_defaults(func=cmd_pretrial_calc)
    p_pre_cons = pre_sub.add_parser("consequences", help="Последствия несоблюдения претензионного порядка.")
    p_pre_cons.add_argument("--code", required=True, choices=["apk", "gpk", "kas"])
    p_pre_cons.add_argument("--stage", required=True, choices=["filing", "trial", "movement"])
    p_pre_cons.set_defaults(func=cmd_pretrial_consequences)

    # update — самопроверка/обновление кода пакета без затрагивания данных (TASK_UPDATER.md)
    p_update = subparsers.add_parser(
        "update",
        help="Check for or perform a sud-cli update; local data (cases, NPA, settings) untouched.",
    )
    p_update.add_argument("--check", action="store_true",
                          help="Only probe update availability; no installation.")
    p_update.add_argument("--source", default=None, choices=["git", "pypi", "local"])
    p_update.add_argument("--target", default=None, help="Explicit source URL or path.")
    p_update.add_argument("--i-am-operator", action="store_true",
                          help="Operator-only: allow package update.")
    p_update.set_defaults(func=cmd_update)

    # draft — генератор процессуальных документов (TASK_DRAFTER_INTEGRATION)
    from .drafter import TEMPLATES as DRAFT_TYPES

    p_draft = subparsers.add_parser(
        "draft",
        help="Генерация процессуального документа (7 типов; ст. 125-260 АПК / 131-322 ГПК).",
    )
    draft_sub = p_draft.add_subparsers(dest="draft_command")
    p_draft_tpl = draft_sub.add_parser(
        "templates", help="Список поддерживаемых типов и их обязательных полей."
    )
    p_draft_tpl.set_defaults(func=cmd_draft)
    p_draft.add_argument("--type", dest="doc_type", default=None, choices=sorted(DRAFT_TYPES),
                         help="Тип процессуального документа (7 типов).")
    p_draft.add_argument("--court", default=None)
    p_draft.add_argument("--plaintiff", default=None)
    p_draft.add_argument("--defendant", default=None)
    p_draft.add_argument("--claim-amount", dest="claim_amount", type=claim_amount_arg, default=None,
                         help="Цена иска в рублях, копейки допустимы.")
    p_draft.add_argument("--basis", default=None, help="Фабула/обоснование (для отсрочки — причина).")
    p_draft.add_argument("--petitum", default=None, help="Просительная часть.")
    p_draft.add_argument("--case-id", dest="case_id", default=None,
                         help="Дело из реестра: подтянуть суд, стороны, номер.")
    p_draft.add_argument("--case-file", dest="case_file", type=Path, default=None,
                         help="JSON с реквизитами документа.")
    p_draft.add_argument("--out", "-o", dest="out", type=Path, default=None,
                         help="Итоговый файл .docx или .md.")
    p_draft.add_argument("--format", dest="format", choices=["docx", "md"], default=None,
                         help="Формат вывода (по умолчанию определяется по расширению --out).")
    p_draft.set_defaults(func=cmd_draft)

    return parser


def _strip_global_options(argv):
    """Return argv with global flags and their values removed.

    This must stay in sync with the global options accepted by build_parser:
    --json, --state-dir PATH, --state-dir=PATH.
    """
    result = []
    it = iter(argv)
    for token in it:
        if token == "--json":
            continue
        elif token == "--state-dir":
            try:
                next(it)
            except StopIteration:
                pass
            continue
        elif token.startswith("--state-dir="):
            continue
        result.append(token)
    return result


def _first_command_token(argv):
    """Return the first positional command token in argv, or None."""
    for token in _strip_global_options(argv):
        if token.startswith("-"):
            continue
        return token
    return None


def _extract_global_options(argv):
    """Extract --json and --state-dir from argv without enforcing full parsing."""
    json_mode = False
    state_dir = None
    it = iter(argv)
    for token in it:
        if token == "--json":
            json_mode = True
        elif token == "--state-dir":
            try:
                state_dir = Path(next(it))
            except StopIteration:
                pass
        elif token.startswith("--state-dir="):
            state_dir = Path(token.split("=", 1)[1])
    return json_mode, state_dir


def _rejected_tokens(argv, limit=8):
    """List the offending tokens so the caller can see what was rejected.

    Path-like tokens are dropped: the suite asserts that no output ever echoes
    $HOME, and an echo of an arbitrary argument is exactly how that leaks. The
    caller still gets the subcommand name, which is what identifies the call.
    """
    kept = []
    for token in argv:
        if len(kept) >= limit:
            break
        # S3-8: path-like отсекается и в форме «--opt=/path» — токен начинается
        # с дефиса, но значение после '=' остаётся абсолютным путем/URL.
        value = token.split("=", 1)[1] if "=" in token and token.startswith("--") else token
        if value.startswith("/") or value.startswith("~") or "://" in value:
            continue
        kept.append(token)
    return " ".join(kept) if kept else "(аргументы не распознаны)"


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    # Backward-compat: legacy "deadlines <key> ..." (v2.4) -> "deadlines calculate <key> ..."
    _DEADLINE_SUBS = {"calculate", "calc", "confirm", "anchor", "waive", "audit", "list", "matrix"}
    # S3-9: переписываем argv только когда «deadlines» — именно команда (первый
    # позиционный токен после глобальных флагов), а не значение чужого аргумента:
    # иначе argv.index ловил «deadlines» где попало и портил легальные вызовы.
    _cmd = _first_command_token(argv)
    if _cmd == "deadlines":
        i = next(idx for idx, t in enumerate(argv) if t == "deadlines")
        if i + 1 < len(argv):
            nxt = argv[i + 1]
            if not nxt.startswith("-") and nxt not in _DEADLINE_SUBS:
                argv = argv[: i + 1] + ["calculate"] + argv[i + 1:]
    # v2.9.0 (S-54): `practice status <n>` routes to the offline fate probe.
    # The live `practice <query>` parser stays flat (MCP drift contract), so
    # the subcommand shape is dispatched here, same precedent as deadlines.
    # S3-9: тот же гейт — «practice» должна быть командой, не чужим значением.
    if _cmd == "practice":
        _ps_i = next(idx for idx, t in enumerate(argv) if t == "practice")
        if _ps_i + 1 < len(argv) and argv[_ps_i + 1] == "status":
            argv = argv[:_ps_i] + ["practice-status"] + argv[_ps_i + 2:]

    json_requested = "--json" in argv
    argv = [token for token in argv if token != "--json"]

    parser = build_parser()
    # A consumer parsing stdout with --json must get parseable output even when
    # the arguments are malformed. argparse exits 2 with human-readable usage
    # on stderr and nothing on stdout, which breaks the machine contract, so in
    # JSON mode we emit a typed error instead. Text mode keeps argparse's own
    # behaviour untouched.
    parse_failed = False
    if json_requested:
        # argparse writes its usage text to stderr the moment it rejects the
        # arguments, which would corrupt the JSON error we are about to emit on
        # the same stream. Swallow it during parsing only — the JSON error is
        # emitted afterwards, outside this block, so it still reaches stderr.
        # In text mode argparse keeps its own behaviour untouched.
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = parser.parse_args(argv)
            except SystemExit:
                parse_failed = True
                args = None
    else:
        args = parser.parse_args(argv)

    if parse_failed:
        bad_command = _first_command_token(argv)
        return error(
            {
                "status": "INVALID_ARGUMENTS",
                "detail": (
                    f"неверные или недостающие аргументы для команды "
                    f"'{bad_command or 'unknown'}': {_rejected_tokens(argv)}"
                ),
            },
            meta={"command": bad_command},
            json_mode=True,
        )

    if args.command is None:
        parser.print_help()
        return 0

    state_dir = args.state_dir or get_state_dir()
    args.state_dir = state_dir
    args.json_mode = json_requested

    try:
        try:
            return args.func(args)
        finally:
            # S3-20: хендлеры CLI открывают SQLite через get_connection и не
            # всегда закрывают (error-path'и, ранний return) — закрываем пакетно.
            from .db import close_all_connections
            close_all_connections()
    except Exception as exc:
        return error(
            f"unexpected internal error: {type(exc).__name__}",
            meta={"command": args.command, "state_dir": str(state_dir)},
            json_mode=args.json_mode,
        )


def cmd_providers(args):
    providers = list_providers()
    payload = [
        {
            "name": p.name,
            "kind": p.kind,
            "status": p.status,
            "description": p.description,
        }
        for p in providers
    ]
    result(
        payload,
        meta={"command": "providers", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_ready(args):
    """Report whether live court commands can run, and how to call them.

    Exits non-zero when something is missing so an agent can gate on it:
    ``sud-cli --json ready || echo "court access not configured"``.
    No court host is contacted — the answer is about configuration only.
    """
    from sud_cli.readiness import check_live_readiness

    report = check_live_readiness()
    ready = bool(report.get("ready"))
    result(
        report,
        meta={
            "command": "ready",
            "ready": ready,
            "state_dir": str(args.state_dir),
        },
        json_mode=args.json_mode,
    )
    return 0 if ready else 1


def cmd_doctor(args):
    from sud_cli.db import doctor_check, init_db

    init_db(args.state_dir)
    report = doctor_check(args.state_dir)
    status = 0 if report.get("healthy") else 1
    result(
        report,
        meta={"command": "doctor", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return status


def cmd_calendar_check(args):
    from .production_calendar import calendar_check

    report = calendar_check()
    result(report, meta={"command": "calendar check"}, json_mode=args.json_mode)
    return 0 if report.get("valid") else 1


def cmd_import(args):
    from sud_cli.db import get_connection, init_db
    from sud_cli.importing import import_fixture

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        report = import_fixture(conn, args.kind, args.file)
    except Exception as exc:
        return error(
            str(exc),
            meta={"command": "import"},
            json_mode=args.json_mode,
        )
    result(
        report,
        meta={"command": "import", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_search(args):
    from sud_cli.db import get_connection, init_db, search_records

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        payload, meta = search_records(
            conn,
            kind=args.kind,
            query=args.query,
            limit=args.limit,
            since=args.since,
            until=args.until,
        )
    except Exception as exc:
        return error(
            str(exc),
            meta={"command": "search", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    meta = meta or {}
    meta.update({"command": "search", "state_dir": str(args.state_dir)})
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def cmd_npa_search(args):
    from sud_cli.db import get_connection, init_db, search_npa

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        payload, meta = search_npa(conn, query=args.query, limit=args.limit)
    except Exception as exc:
        return error(
            str(exc),
            meta={"command": "npa search", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    meta = meta or {}
    meta.update({"command": "npa search", "state_dir": str(args.state_dir)})
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def cmd_npa_compare(args):
    from sud_cli.db import get_connection, init_db, compare_npa_editions

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        payload = compare_npa_editions(
            conn, doc_id=args.doc_id, edition1=args.edition1, edition2=args.edition2
        )
    except Exception as exc:
        return error(
            str(exc),
            meta={"command": "npa compare", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        payload,
        meta={"command": "npa compare", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_npa_download(args):
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "npa download", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.npa_downloader import NpaDownloadError, download_npa

    try:
        payload = download_npa(
            eo_number=args.eo_number,
            url=args.url,
            source=args.source,
            case_num=args.case_num,
            out_dir=args.out_dir or (args.state_dir / "npa"),
            extract_text=args.extract_text,
        )
    except NpaDownloadError as exc:
        return error(
            f"DOWNLOAD_FAILED: {exc}",
            meta={"command": "npa download", "state_dir": str(args.state_dir),
                  "warning": LIVE_WARNING_MSG},
            json_mode=args.json_mode,
        )
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["warning"] = LIVE_WARNING_MSG
        if isinstance(payload.get("meta"), dict):
            payload["meta"] = dict(payload["meta"])
            payload["meta"]["warning"] = LIVE_WARNING_MSG
    import sys as _sys
    print(LIVE_WARNING_MSG, file=_sys.stderr)
    result(
        payload,
        meta={"command": "npa download", "state_dir": str(args.state_dir),
              "warning": LIVE_WARNING_MSG},
        json_mode=args.json_mode,
    )
    return 0


def cmd_law_search(args):
    from sud_cli.pravo_publication import PublicationError, query_publication_metadata

    try:
        payload = query_publication_metadata(query=args.query, number=args.number)
    except PublicationError as exc:
        return error(
            str(exc),
            meta={"command": "law search"},
            json_mode=args.json_mode,
        )
    meta = {
        "command": "law search",
        "state_dir": str(args.state_dir),
        "source": payload["source"],
        "provider_label": payload["provider_label"],
        "page": payload["pagination"]["page"],
        "page_size": payload["pagination"]["page_size"],
        "total": payload["pagination"]["total"],
        "pages_total": payload["pagination"]["pages_total"],
    }
    result(payload["items"], meta=meta, json_mode=args.json_mode)
    return 0


def cmd_court_import(args):
    from sud_cli.db import get_connection, init_db
    from sud_cli.importing import CourtImportError, ImportError as FixtureImportError
    from sud_cli.importing import import_court_cases

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        report = import_court_cases(conn, args.file)
    except (CourtImportError, FixtureImportError) as exc:
        return error(
            str(exc),
            meta={"command": "court import"},
            json_mode=args.json_mode,
        )
    except Exception as exc:
        return error(
            f"unexpected internal error: {type(exc).__name__}",
            meta={"command": "court import"},
            json_mode=args.json_mode,
        )
    result(
        report,
        meta={"command": "court import", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_court_search(args):
    from sud_cli.db import SearchError, get_connection, init_db, search_court_cases

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        payload, meta = search_court_cases(
            conn,
            query=args.query,
            limit=args.limit,
            since=args.since,
            until=args.until,
        )
    except SearchError as exc:
        return error(
            str(exc),
            meta={"command": "court search"},
            json_mode=args.json_mode,
        )
    except Exception as exc:
        return error(
            f"unexpected internal error: {type(exc).__name__}",
            meta={"command": "court search"},
            json_mode=args.json_mode,
        )
    meta = meta or {}
    meta.update({"command": "court search", "state_dir": str(args.state_dir)})
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def cmd_court_case(args):
    from sud_cli.db import get_case_events, get_case_summary, get_connection, init_db

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        summary = get_case_summary(conn, args.case_id)
        if summary is None:
            return error(
                f"case not found: {args.case_id}",
                meta={"command": "court case"},
                json_mode=args.json_mode,
            )
        events = get_case_events(conn, args.case_id)
        payload = {
            **summary,
            "events": events,
            "event_count": len(events),
        }
    except Exception as exc:
        return error(
            f"unexpected internal error: {type(exc).__name__}",
            meta={"command": "court case", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        payload,
        meta={"command": "court case", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def _emit_bridge_result(bridge_result, label, state_dir, json_mode):
    """Emit a bridge result under its own status and return the exit code.

    The bridge distinguishes ``blocked`` ("live access is not enabled", nothing
    was attempted) from ``error`` (something was attempted and failed). Routing
    both through ``error`` told an agent that a missing env var was a crash, so
    it retried a call that could never succeed.
    """
    status = bridge_result.get("status")
    if status == "ok":
        result(
            bridge_result["data"],
            meta={"command": label, "state_dir": str(state_dir)},
            json_mode=json_mode,
        )
        return 0
    emitter = blocked if status == "blocked" else error
    emitter(
        bridge_result["data"],
        meta={"command": label},
        json_mode=json_mode,
    )
    return 1


def cmd_court_watcher_providers(args):
    bridge_result = run_bridge_command(
        "providers",
        state_dir=args.state_dir,
    )
    return _emit_bridge_result(
        bridge_result, "court-watcher providers", args.state_dir, args.json_mode
    )


def cmd_court_watcher_import(args):
    bridge_result = run_bridge_command(
        args.cw_command,
        state_dir=args.state_dir,
        input_path=args.file,
        case_id=args.case_id,
    )
    return _emit_bridge_result(
        bridge_result,
        f"court-watcher {args.cw_command}",
        args.state_dir,
        args.json_mode,
    )


def cmd_court_watcher_extract(args):
    bridge_result = run_bridge_command(
        "extract-text",
        state_dir=args.state_dir,
        input_path=args.file,
    )
    return _emit_bridge_result(
        bridge_result, "court-watcher extract-text", args.state_dir, args.json_mode
    )


def cmd_case_diff(args):
    """Compare two saved snapshots of the same case.

    Offline by construction: it reads two JSON files and touches no court
    source, so it needs no live gate and no license. The compared field set
    mirrors ``case_search.diff._SCALARS``, which court-watcher computes
    internally but never exposes.
    """
    from sud_cli.case_diff import DiffError, diff_case_snapshots

    try:
        payload = diff_case_snapshots(args.old, args.new)
    except DiffError as exc:
        return error(
            {"status": "DIFF_ERROR", "detail": str(exc)},
            meta={"command": "court-watcher case-diff"},
            json_mode=args.json_mode,
        )

    result(
        payload,
        meta={
            "command": "court-watcher case-diff",
            "changed": payload["changed"],
        },
        json_mode=args.json_mode,
    )
    return 0


def cmd_court_watcher_parity(args):
    from sud_cli.court_watcher_bridge import get_parity_matrix

    matrix = get_parity_matrix()
    result(
        matrix,
        meta={"command": "court-watcher parity", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def _run_flag_command_cli(args, command, options, label):
    """Shared handler for the Phase 8 flag-driven live bridge commands."""
    bridge_result = run_bridge_command(
        command,
        state_dir=args.state_dir,
        options=options,
    )
    return _emit_bridge_result(bridge_result, label, args.state_dir, args.json_mode)


def cmd_court_watcher_search_practice(args):
    # sudact.ru serves a "Поиск не доступен" stub to plain HTTP clients, so the
    # search only returns results when it goes through the Camofox browser. The
    # flag therefore defaults on; --no-camofox is the explicit opt-out.
    via_camofox = not args.no_camofox
    return _run_flag_command_cli(
        args,
        "search-practice",
        {
            "query": args.query,
            "section": args.section,
            "page": args.page,
            "limit": args.limit,
            "via_camofox": via_camofox,
        },
        "court-watcher search-practice",
    )


def _run_live_command_cli(args, command, label, **kwargs):
    """Shared handler for positional live commands reached through the bridge."""
    bridge_result = run_bridge_command(
        command,
        state_dir=args.state_dir,
        **kwargs,
    )
    return _emit_bridge_result(bridge_result, label, args.state_dir, args.json_mode)


def cmd_court_watcher_lookup(args):
    return _run_live_command_cli(
        args,
        "lookup",
        "court-watcher lookup",
        case_id=args.case_id,
        options={"--source": args.source} if args.source else None,
    )


def cmd_court_watcher_case_history(args):
    return _run_live_command_cli(
        args, "case-history", "court-watcher case-history", case_id=args.case_id
    )


def cmd_court_watcher_sudrf_live(args):
    return _run_live_command_cli(
        args, "sudrf-live", "court-watcher sudrf-live", case_id=args.case_id
    )


def cmd_court_watcher_mos_live(args):
    return _run_live_command_cli(
        args,
        "mos-live",
        "court-watcher mos-live",
        case_id=args.case_id,
        court_slug=args.court_slug,
    )


def cmd_court_watcher_kad_search(args):
    return _run_flag_command_cli(
        args,
        "kad-search",
        {
            "inn": args.inn,
            "ogrn": args.ogrn,
            "name": args.name,
            "page": args.page,
        },
        "court-watcher kad-search",
    )


def cmd_court_watcher_get_text(args):
    return _run_flag_command_cli(
        args,
        "get-text",
        {
            "url_or_file": args.url,
            "no_cache": args.no_cache,
            "blocks": args.blocks,
        },
        "court-watcher get-text",
    )


def _emit_native(outcome, label, args, warning=None):
    payload = outcome.to_dict() if hasattr(outcome, "to_dict") else outcome
    if warning and isinstance(payload, dict):
        payload = dict(payload)
        payload["warning"] = warning
    status = payload.get("status", "error")
    emitter = result if status == "ok" else (blocked if status == "degraded" else error)
    meta = {"command": label, "state_dir": str(args.state_dir)}
    if warning:
        meta["warning"] = warning
    emitter(payload, meta=meta, json_mode=args.json_mode)
    if warning and status == "ok":
        import sys as _sys
        print(warning, file=_sys.stderr)
    return 0 if status == "ok" else 1


def cmd_native_practice(args):
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "practice", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.case_search import search_practice
    return _emit_native(search_practice(args.query, section=args.section, page=args.page, limit=args.limit), "practice", args, warning=LIVE_WARNING_MSG)


def cmd_native_kad(args):
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "kad", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.case_search import kad_search
    return _emit_native(kad_search(args.inn, args.ogrn, args.name, page=args.page), "kad", args, warning=LIVE_WARNING_MSG)


def cmd_native_case_history(args):
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "case-history", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.case_search import case_history
    return _emit_native(case_history(args.case_id, section=args.section, max_related=args.max_related, limit=args.limit), "case-history", args, warning=LIVE_WARNING_MSG)


def cmd_native_sudrf(args):
    # S2-2: гейт live-источника, как у kad/case-history/extract-text —
    # без него команда шла в сеть мимо SUD_CLI_LIVE_OK.
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "sudrf-live", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.case_search import get_sudrf_case
    return _emit_native(get_sudrf_case(args.case_id, host=args.host), "sudrf-live", args, warning=LIVE_WARNING_MSG)


def cmd_native_mos(args):
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "mos-live", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.case_search import get_mos_case
    return _emit_native(get_mos_case(args.court_slug, args.case_id), "mos-live", args, warning=LIVE_WARNING_MSG)


def cmd_efrsb(args):
    """ЕФРСБ (bankrot.fedresurs.ru): банкротные проверки. Not_found — валидный
    ответ (обёртка ok), ошибка транспорта/параметров — error с exit 1."""
    from sud_cli import efrsb_public as ep

    try:
        if args.efrsb_cmd == "companies":
            payload = ep.search_companies(inn=args.inn, ogrn=args.ogrn,
                                          name=args.name, limit=args.limit)
        elif args.efrsb_cmd == "persons":
            payload = ep.search_persons(args.name, birth_date=args.birth_date,
                                        limit=args.limit)
        elif args.efrsb_cmd == "publications":
            payload = ep.case_publications(args.case_id, limit=args.limit)
        else:
            payload = ep.search_managers(search=args.search, inn=args.inn,
                                         sro=args.sro, limit=args.limit)
    except ep.EfrsbPublicError as exc:
        error(str(exc), meta={"command": "efrsb"}, json_mode=args.json_mode)
        return 1
    result(payload,
           meta={"command": "efrsb", "source": "bankrot.fedresurs.ru"},
           json_mode=args.json_mode)
    return 0


def cmd_native_text(args):
    if not _live_ok():
        return error(LIVE_DISABLED_MSG, meta={"command": "extract-text", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    from sud_cli.case_search import get_decision_text
    return _emit_native(get_decision_text(str(args.file)), "extract-text", args, warning=LIVE_WARNING_MSG)


def cmd_native_import_card(args):
    from sud_cli.case_search import import_document
    return _emit_native(import_document(args.file, args.case_id, store_dir=args.state_dir / "snapshots"), "import-card", args)


def _render_disclaimer(rules, calc_date):
    """The specification makes the reference-only disclaimer mandatory."""
    return rules["mandatory_disclaimer"].format(calc_date=calc_date)


def _print_disclaimer(disclaimer, json_mode=False):
    # Printed to stderr so it survives JSON mode: a consumer parsing stdout
    # must still be able to show the user that this is advisory, not advice.
    #
    # In --json mode it must NOT be printed as bare text: a caller that parses
    # stderr as JSON would choke on the Russian notice, and the project's
    # convention is typed output on both streams. The disclaimer is still
    # carried — in the successful payload as ``disclaimer``, and for refusals
    # the caller gets a JSON envelope with the same rule metadata. Skipping it
    # here keeps the stream machine-readable; the notice is not lost, it moves
    # into the JSON.
    if json_mode:
        return
    print(disclaimer, file=sys.stderr)


def cmd_fees(args):
    from datetime import date as _date

    from .legal_engine import (
        STATUS_OK,
        calculate_fee,
        load_rules,
        validate_claim_class,
    )

    try:
        rules = load_rules()
    except Exception as exc:  # rules file missing/corrupt must fail closed
        error(
            {"status": "RULES_UNAVAILABLE", "detail": str(exc)},
            meta={"command": "fees"},
            json_mode=args.json_mode,
        )
        return 1

    if args.claim_class is not None:
        class_result = validate_claim_class(args.claim_class, rules, args.kind)
        if class_result.status != STATUS_OK:
            error(
                class_result.to_dict(),
                meta={"command": "fees", "rules_version": rules["version"]},
                json_mode=args.json_mode,
            )
            return 1

    fee = calculate_fee(
        claim_amount=args.claim_amount,
        jurisdiction=args.jurisdiction,
        fee_kind=args.kind,
        applicant_type=args.applicant,
        rules=rules,
        as_of=getattr(args, "as_of", None),
    )

    calc_date = getattr(args, "as_of", None) or _date.today().isoformat()
    _print_disclaimer(_render_disclaimer(rules, calc_date), args.json_mode)

    meta = {
        "command": "fees",
        "rules_version": rules["version"],
        "tariff_version": rules["tariff_version"],
        "calc_date": calc_date,
    }
    if getattr(args, "as_of", None):
        meta["as_of"] = args.as_of

    if fee.status != STATUS_OK:
        error(fee.to_dict(), meta=meta, json_mode=args.json_mode)
        return 1

    payload = fee.to_dict()
    payload["disclaimer"] = _render_disclaimer(rules, calc_date)
    payload["exemptions_note"] = rules["exemptions"]["mandatory_note"]
    payload["clean_room"] = rules["clean_room"]
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def _run_deadline_calculation(args):
    from datetime import date as _date, datetime, timezone

    from .legal_deadlines import calculate_deadline
    from .legal_engine import STATUS_OK, load_rules
    from sud_cli.db import (
        get_latest_anchored_state,
        insert_deadline_state,
    )

    try:
        rules = load_rules()
    except Exception as exc:
        error(
            {"status": "RULES_UNAVAILABLE", "detail": str(exc)},
            meta={"command": "deadlines"},
            json_mode=args.json_mode,
        )
        return 1

    if getattr(args, "calc_case_id", None) and not getattr(args, "case_id", None):
        args.case_id = args.calc_case_id
    if not getattr(args, "deadline_key", None):
        args.deadline_key = getattr(args, "calc_deadline_key", None)

    event_date = None
    if getattr(args, "event_date", None):
        try:
            event_date = datetime.strptime(args.event_date, "%Y-%m-%d").date()
        except ValueError:
            error(
                {
                    "status": "MISSING_REQUIRED_PARAM",
                    "detail": f"event_date '{args.event_date}' не в формате ГГГГ-ММ-ДД",
                },
                meta={"command": "deadlines"},
                json_mode=args.json_mode,
            )
            return 1

    case_id_for_anchor = getattr(args, "case_id", None)
    if event_date is None and case_id_for_anchor:
        try:
            from .db import get_connection, get_latest_anchored_state, init_db
            init_db(args.state_dir)
            conn = get_connection(args.state_dir)
            try:
                anchored = get_latest_anchored_state(conn, case_id_for_anchor, args.deadline_key)
            finally:
                conn.close()
            if anchored:
                ab = anchored.get("basis") or {}
                if isinstance(ab, dict) and ab.get("event_date"):
                    try:
                        event_date = datetime.strptime(str(ab["event_date"]), "%Y-%m-%d").date()
                        args.event_date = str(ab["event_date"])
                    except ValueError:
                        pass
        except Exception:
            pass

    deadline = calculate_deadline(
        deadline_key=args.deadline_key,
        event_date=event_date,
        rules=rules,
        region=getattr(args, "region", None),
        tz=getattr(args, "tz", None) or "Europe/Moscow",
    )

    calc_date = _date.today().isoformat()
    _print_disclaimer(_render_disclaimer(rules, calc_date), args.json_mode)

    meta = {
        "command": "deadlines",
        "rules_version": rules["version"],
        "calc_date": calc_date,
        "open_questions": rules["spec_open_questions"],
    }

    if deadline.status != STATUS_OK:
        error(deadline.to_dict(), meta=meta, json_mode=args.json_mode)
        return 1

    case_id = getattr(args, "case_id", None)
    warning_basis = None
    state = "computed"
    confirmed_by = None

    if case_id:
        from sud_cli.db import get_connection, init_db
        init_db(args.state_dir)
        conn = get_connection(args.state_dir)
        try:
            anchored = get_latest_anchored_state(conn, case_id, args.deadline_key)
            if anchored is None:
                warning_basis = "event_date_assumed"
            basis = anchored.get("basis") if anchored else {
                "event_date": args.event_date,
                "assumed": True,
                "actor": "calculator",
                "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
            # calculate due_at already
            due_at = deadline.deadline.isoformat() if deadline.deadline else None
            row = insert_deadline_state(
                conn, case_id, args.deadline_key, due_at=due_at, basis=basis,
                state="computed", state_dir=None,
            )
            if getattr(args, "confirm", False):
                actor = getattr(args, "by", None) or "calculator"
                from sud_cli.db import confirm_deadline_state
                row = confirm_deadline_state(conn, args.state_dir, case_id, args.deadline_key, actor,
                                            evidence=getattr(args, "evidence", None))
                state = "confirmed"
                confirmed_by = actor
            # if waive requested
            if getattr(args, "waive", False) and getattr(args, "norm", None):
                actor = getattr(args, "by", None) or "calculator"
                from sud_cli.db import waive_deadline_state
                row = waive_deadline_state(conn, args.state_dir, case_id, args.deadline_key, actor,
                                          getattr(args, "norm"), evidence=getattr(args, "evidence", None))
                state = "waived"
                confirmed_by = actor
        finally:
            conn.close()

    payload = deadline.to_dict()
    payload["disclaimer"] = _render_disclaimer(rules, calc_date)
    payload["clean_room"] = rules["clean_room"]
    if case_id:
        payload["state"] = state
        payload["confirmed_by"] = confirmed_by
        if warning_basis:
            payload["warning_basis"] = warning_basis
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def cmd_deadlines(args):
    sub = getattr(args, "deadlines_command", None)
    if sub in ("calculate", "calc"):
        # normalize positional
        if getattr(args, "calc_case_id", None) and not getattr(args, "case_id", None):
            args.case_id = args.calc_case_id
        if not getattr(args, "deadline_key", None):
            args.deadline_key = getattr(args, "calc_deadline_key", None)
        # map event_date already
        return _run_deadline_calculation(args)
    if sub == "confirm":
        return cmd_deadlines_confirm(args)
    if sub == "anchor":
        return cmd_deadlines_anchor(args)
    if sub == "waive":
        return cmd_deadlines_waive(args)
    if sub == "audit":
        return cmd_deadlines_audit(args)
    if sub == "list":
        return cmd_deadlines_list(args)
    if sub == "matrix":
        return cmd_deadlines_matrix(args)

    # legacy: if no deadline_key but a case_id-like first? handle
    if getattr(args, "deadline_key", None) is None:
        # maybe called as: deadlines <case_id> <key>? not supported; error
        error({"status": "MISSING_REQUIRED_PARAM", "detail": "deadline_key required"}, meta={"command": "deadlines"}, json_mode=args.json_mode)
        return 1

    # support calculate with case_id in legacy positional? If deadline_key looks like case? no.
    return _run_deadline_calculation(args)


def cmd_deadlines_calculate(args):
    if getattr(args, "calc_case_id", None) and not getattr(args, "case_id", None):
        args.case_id = args.calc_case_id
    if not getattr(args, "deadline_key", None):
        args.deadline_key = getattr(args, "calc_deadline_key", None)
    return _run_deadline_calculation(args)


def cmd_deadlines_confirm(args):
    from sud_cli.db import confirm_deadline_state, get_connection, init_db
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        if getattr(args, "waive", False) or getattr(args, "norm", None):
            if not getattr(args, "norm", None):
                return error({"status": "INVALID_ARGUMENTS", "detail": "norm required for waive"}, meta={"command": "deadlines confirm"}, json_mode=args.json_mode)
            row = confirm_deadline_state(conn, args.state_dir, args.case_id, args.key, args.by,
                                        evidence=getattr(args, "evidence", None), waive=True, norm=args.norm)
        else:
            row = confirm_deadline_state(conn, args.state_dir, args.case_id, args.key, args.by,
                                        evidence=getattr(args, "evidence", None))
    finally:
        conn.close()
    result(row, meta={"command": "deadlines confirm", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_deadlines_anchor(args):
    from sud_cli.db import anchor_deadline_state, get_connection, init_db
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        actor = getattr(args, "by", None) or "cli"
        try:
            row = anchor_deadline_state(conn, args.state_dir, args.case_id, args.key,
                                       args.event_date, args.method, args.evidence, actor=actor)
        except ValueError as exc:
            return error(
                {"status": "invalid_deadline_key", "detail": str(exc)},
                meta={"command": "deadlines anchor", "state_dir": str(args.state_dir)},
                json_mode=args.json_mode,
            )
    finally:
        conn.close()
    result(row, meta={"command": "deadlines anchor", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_deadlines_waive(args):
    from sud_cli.db import get_connection, init_db, waive_deadline_state
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        row = waive_deadline_state(conn, args.state_dir, args.case_id, args.key, args.by, args.norm,
                                  evidence=getattr(args, "evidence", None))
    finally:
        conn.close()
    result(row, meta={"command": "deadlines waive", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_deadlines_audit(args):
    from sud_cli.db import get_connection, init_db, read_deadline_audit
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        entries = read_deadline_audit(args.state_dir, args.case_id)
    finally:
        conn.close()
    result(entries, meta={"command": "deadlines audit", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_deadlines_list(args):
    from sud_cli.db import get_connection, init_db, list_all_deadline_states, list_deadline_states
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        if getattr(args, "case_id", None):
            rows = list_deadline_states(conn, args.case_id)
        else:
            rows = list_all_deadline_states(conn)
    finally:
        conn.close()
    result(rows, meta={"command": "deadlines list", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_deadlines_matrix(args):
    from datetime import date as _date

    from .deadline_matrix import build_matrix
    from .legal_engine import load_rules

    case_id = getattr(args, "case_id", None)
    if not case_id:
        return error({"status": "MISSING_REQUIRED_PARAM", "detail": "case_id required"},
                    meta={"command": "deadlines matrix"}, json_mode=args.json_mode)

    try:
        rules = load_rules()
    except Exception as exc:
        return error({"status": "RULES_UNAVAILABLE", "detail": str(exc)},
                    meta={"command": "deadlines matrix"}, json_mode=args.json_mode)

    try:
        instance, points = build_matrix(args.state_dir, case_id,
                                       instance_override=getattr(args, "instance", None),
                                       rules=rules, as_of=getattr(args, "as_of", None))
    except ValueError as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)},
                    meta={"command": "deadlines matrix", "state_dir": str(args.state_dir)},
                    json_mode=args.json_mode)

    if getattr(args, "missing", False):
        points = [p for p in points if p.get("needs_anchor")]

    calc_date = _date.today().isoformat()
    disclaimer = _render_disclaimer(rules, calc_date)
    _print_disclaimer(disclaimer, args.json_mode)

    payload = {
        "case_id": case_id,
        "instance": instance,
        "as_of": getattr(args, "as_of", None),
        "points": points,
        "matrix": points,
        "items": points,
        "deadlines": points,
        "entries": points,
        "disclaimer": disclaimer,
        "clean_room": rules.get("clean_room"),
    }
    meta = {
        "command": "deadlines matrix",
        "rules_version": rules["version"],
        "calc_date": calc_date,
        "state_dir": str(args.state_dir),
    }
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def cmd_calendar_agenda(args):
    from datetime import date as _date, timedelta
    from sud_cli.db import get_connection, init_db, list_all_deadline_states, list_cases

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        states = list_all_deadline_states(conn)
        cases = {c["case_id"]: c for c in list_cases(conn)}
        hearing_rows = conn.execute(
            "SELECT * FROM events WHERE lower(event_type) LIKE '%hearing%' OR lower(event_type) LIKE '%слушани%'"
        ).fetchall()
        hearing_events = [dict(r) for r in hearing_rows]
    finally:
        conn.close()

    today = _date.today()
    horizon = today + timedelta(days=getattr(args, "days", 14))
    items = []
    for s in states:
        if s.get("state") not in ("computed", "confirmed"):
            continue
        if not s.get("due_at"):
            continue
        case = cases.get(s.get("case_id"))
        if case and case.get("status") == "closed":
            continue
        try:
            due = _date.fromisoformat(str(s["due_at"])[:10])
        except ValueError:
            continue
        items.append({
            "kind": "deadline",
            "case_id": s.get("case_id"),
            "deadline_key": s.get("deadline_key"),
            "due_at": s.get("due_at"),
            "state": s.get("state"),
            "confirmed_by": s.get("confirmed_by"),
            "evidence_note": s.get("evidence_note"),
        })
    for e in hearing_events:
        ed = e.get("event_date")
        if not ed:
            continue
        try:
            due = _date.fromisoformat(str(ed)[:10])
        except ValueError:
            continue
        items.append({
            "kind": "hearing",
            "case_id": e.get("case_id"),
            "event_id": e.get("event_id"),
            "due_at": ed,
            "state": "scheduled",
            "title": e.get("doc_text"),
            "confirmed_by": None,
        })

    def _due(x):
        try:
            return _date.fromisoformat(str(x["due_at"])[:10])
        except Exception:
            return today

    overdue = sorted([i for i in items if _due(i) < today], key=_due)
    upcoming = sorted([i for i in items if today <= _due(i) <= horizon], key=_due)
    next_5 = upcoming[:5]
    payload = {
        "horizon_days": getattr(args, "days", 14),
        "today": today.isoformat(),
        "overdue": overdue,
        "upcoming": upcoming,
        "next_5": next_5,
        "next": next_5,
        "items": sorted(items, key=_due),
        "hearings": [i for i in items if i.get("kind") == "hearing"],
        "empty": len(items) == 0,
    }
    result(payload, meta={"command": "calendar agenda", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0
def cmd_interest(args):
    from datetime import datetime

    from .interest import calculate_interest

    try:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    except ValueError:
        return error({"status": "MISSING_REQUIRED_PARAM", "detail": "dates must use YYYY-MM-DD"}, meta={"command": "interest"}, json_mode=args.json_mode)
    calc = calculate_interest(args.amount, start, end, args.article, args.payments)
    payload = calc.to_dict()
    if args.calc_fee and calc.status == "ok":
        from .legal_engine import calculate_fee, load_rules
        # S1-15: суммирование в Decimal с одним усечением; двойное int() теряло
        # дробные части обоих слагаемых до сложения.
        claim_amount = int(calc.remaining_principal + calc.amount_rub)
        fee = calculate_fee(claim_amount=claim_amount, jurisdiction=args.jurisdiction, fee_kind=args.kind, applicant_type=args.applicant, rules=load_rules())
        payload.update({"principal_rub": args.amount, "claim_amount_rub": claim_amount, "fee": fee.to_dict()})
    emitter = result if calc.status == "ok" else error
    if emitter is error:
        return emitter(payload, meta={"command": "interest"}, json_mode=args.json_mode)
    emitter(payload, meta={"command": "interest"}, json_mode=args.json_mode)
    return 0


def cmd_payment(args):
    from .payment import payment_details
    try:
        payload = payment_details(args.jurisdiction, args.amount, args.court, args.defendant)
    except ValueError as exc:
        return error(str(exc), meta={"command": "payment"}, json_mode=args.json_mode)
    result(payload, meta={"command": "payment"}, json_mode=args.json_mode)
    return 0


def cmd_fee_exemption(args):
    from .fees_extra import fee_exemption_check
    from .legal_engine import load_rules
    try:
        rules = load_rules()
    except Exception as exc:
        return error({"status": "RULES_UNAVAILABLE", "detail": str(exc)},
                     meta={"command": "fee-exemption"}, json_mode=args.json_mode)
    payload = fee_exemption_check(rules, jurisdiction=args.jurisdiction, category_code=args.category)
    emitter = result if payload.get("status") == "exempt" else (error if payload.get("status") == "manual_review" else result)
    return emitter(payload, meta={"command": "fee-exemption", "rules_version": rules["version"]},
                   json_mode=args.json_mode) or 0


def cmd_fee_refund(args):
    from .fees_extra import fee_refund
    from .legal_engine import load_rules
    from decimal import Decimal
    try:
        rules = load_rules()
        paid = Decimal(args.paid)
        # S3-20: assert вырезается python -O; валидация — явная, не assert'ом.
        if paid < 0:
            raise ValueError("paid must be >= 0")
    except Exception as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)},
                     meta={"command": "fee-refund"}, json_mode=args.json_mode)
    payload = fee_refund(rules, paid=paid, ground=args.ground,
                         claim_admitted=args.claim_admitted,
                         plaintiff_default=args.plaintiff_default,
                         transferred_to_arbitration=args.transferred_to_arbitration)
    emitter = result if payload.get("status") == "ok" else error
    return emitter(payload, meta={"command": "fee-refund", "rules_version": rules["version"]},
                   json_mode=args.json_mode) or 0


def cmd_fee_indexation(args):
    from .fees_extra import indexation_compute, indexation_frame
    from .legal_engine import load_rules
    from decimal import Decimal
    try:
        rules = load_rules()
        amount = Decimal(args.amount) if args.amount else None
        cs = Decimal(args.cpi_start) if args.cpi_start else None
        ce = Decimal(args.cpi_end) if args.cpi_end else None
    except Exception as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)},
                     meta={"command": "fee-indexation"}, json_mode=args.json_mode)
    payload = (indexation_compute(rules, code=args.code, amount=amount, cpi_start=cs, cpi_end=ce)
               if args.amount else indexation_frame(rules, code=args.code))
    emitter = result if payload.get("status") in ("ok", "frame_only") else error
    return emitter(payload, meta={"command": "fee-indexation", "rules_version": rules["version"]},
                   json_mode=args.json_mode) or 0


def cmd_deferral(args):
    from .deferral import generate_deferral
    payload = generate_deferral(args.applicant, args.court, args.amount,
                                args.reason, court_type=args.court_type)
    if payload["status"] != "ok":
        return error(payload, meta={"command": "deferral"}, json_mode=args.json_mode)
    result(payload, meta={"command": "deferral"}, json_mode=args.json_mode)
    return 0


def cmd_limitation(args):
    from datetime import date
    from .limitation import calculate_limitation
    try:
        start = date.fromisoformat(args.start)
        check_date = date.fromisoformat(args.check_date) if args.check_date else date.today()
        payload = calculate_limitation(start=start, period_type=args.period_type,
            claim_notice_days=args.claim_notice_days, check_date=check_date,
            custom_years=args.years, custom_months=args.months,
            pretrial_start=date.fromisoformat(args.pretrial_start) if args.pretrial_start else None,
            pretrial_end=date.fromisoformat(args.pretrial_end) if args.pretrial_end else None).to_dict()
    except ValueError as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)}, meta={"command": "limitation"}, json_mode=args.json_mode)
    emitter = result if payload["status"] == "ok" else error
    return emitter(payload, meta={"command": "limitation"}, json_mode=args.json_mode) or 0


def cmd_timeline_audit(args):
    import json
    from .timeline_audit import audit_timeline
    try:
        source = Path(args.events)
        raw = json.loads(source.read_text(encoding="utf-8")) if source.exists() else json.loads(args.events)
        events = raw.get("events", raw) if isinstance(raw, dict) else raw
        payload = audit_timeline(events)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)}, meta={"command": "timeline-audit"}, json_mode=args.json_mode)
    result(payload, meta={"command": "timeline-audit"}, json_mode=args.json_mode)
    return 0


def cmd_npa_as_of(args):
    from .npa_as_of import lookup_as_of
    payload = lookup_as_of(args.act, args.article, args.date)
    if payload.get("status") != "ok":
        return error(payload, meta={"command": "npa-as-of"}, json_mode=args.json_mode)
    result(payload, meta={"command": "npa-as-of"}, json_mode=args.json_mode)
    return 0


def cmd_verify_quote(args):
    """Закон Zero (S-52): цитата подтверждается только против явного источника.

    exact/fuzzy/not_found/edition_unavailable — это данные (exit 0); кривые
    аргументы — INVALID_ARGUMENTS (exit != 0). Сети нет ни в одном источнике.
    """
    from .verify import VerifyInputError, verify_quote
    try:
        payload = verify_quote(
            args.text, args.source,
            act=args.act, article=args.article, date=args.date,
            case_id=args.case_id, path=args.path,
            state_dir=args.state_dir, max_fuzzy=args.max_fuzzy,
        )
    except VerifyInputError as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)},
                     meta={"command": "verify quote"}, json_mode=args.json_mode)
    except UnicodeDecodeError as exc:
        return error({"status": "INVALID_ARGUMENTS",
                      "detail": f"source file is not valid UTF-8: {exc}"},
                     meta={"command": "verify quote"}, json_mode=args.json_mode)
    except OSError as exc:
        return error({"status": "INVALID_ARGUMENTS",
                      "detail": f"source file unreadable: {exc}"},
                     meta={"command": "verify quote"}, json_mode=args.json_mode)
    result(payload, meta={"command": "verify quote"}, json_mode=args.json_mode)
    return 0


def cmd_practice_status(args):
    """S-54: verdict только из локальных артефактов; «действует» не выдается."""
    from .practice_status import assess_status
    store = args.store
    if store is None:
        candidate = args.state_dir / "court-watcher-snapshots"
        store = candidate if candidate.is_dir() else None
    try:
        payload = assess_status(args.case_number, related=args.related, store_dir=store)
    except ValueError as exc:
        return error({"status": "INVALID_ARGUMENTS", "detail": str(exc)},
                     meta={"command": "practice status"}, json_mode=args.json_mode)
    result(payload, meta={"command": "practice status"}, json_mode=args.json_mode)
    return 0


def cmd_serve_mcp(args):
    from .mcp_server import serve_stdio
    serve_stdio()
    return 0


LEGAL_CASE_STATUSES = ["new", "review", "filed", "pending", "decided", "appealed", "closed", "unknown"]


def cmd_case_new(args):
    from sud_cli.db import create_case, get_connection, init_db
    if args.status is not None and args.status not in LEGAL_CASE_STATUSES:
        return error(
            {"status": "invalid_status", "detail": f"status {args.status!r} not allowed"},
            meta={"command": "case new", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        res = create_case(
            conn, args.number, args.court, args.plaintiff, args.defendant,
            owner=args.owner, status=args.status,
        )
    except Exception as exc:
        return error(
            str(exc),
            meta={"command": "case new", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    finally:
        conn.close()
    if res.get("duplicate"):
        result(
            {"status": "duplicate_window", "case_id": res["case_id"]},
            meta={"command": "case new", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
        return 0
    result(
        {"case_id": res["case_id"], "case_number": res.get("case_number"),
         "court": res.get("court"), "status": res.get("status"),
         "owner": res.get("owner")},
        meta={"command": "case new", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_set(args):
    from sud_cli.db import get_connection, init_db, set_case
    if args.status is not None and args.status not in LEGAL_CASE_STATUSES:
        return error(
            {"status": "invalid_status", "detail": f"status {args.status!r} not allowed"},
            meta={"command": "case set", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        fields = {}
        if args.status is not None:
            fields["status"] = args.status
        if args.owner is not None:
            fields["owner"] = args.owner
        if args.closed_at is not None:
            fields["closed_at"] = args.closed_at
        updated = set_case(conn, args.case_id, **fields)
    except Exception as exc:
        return error(
            {"status": "invalid_status" if "invalid_status" in str(exc) else str(exc),
             "detail": str(exc)},
            meta={"command": "case set", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    finally:
        conn.close()
    if updated is None:
        return error(
            f"case not found: {args.case_id}",
            meta={"command": "case set", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        updated,
        meta={"command": "case set", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_amount_add(args):
    from sud_cli.db import InvalidAmountError, add_case_amount, get_connection, init_db
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        try:
            res = add_case_amount(conn, args.case_id, args.kind, args.amount, note=args.note)
        except InvalidAmountError as exc:
            return error(
                {"status": "invalid_amount", "detail": str(exc)},
                meta={"command": "case amount add", "state_dir": str(args.state_dir)},
                json_mode=args.json_mode,
            )
    finally:
        conn.close()
    if res is None:
        return error(
            f"case not found: {args.case_id}",
            meta={"command": "case amount add", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        res,
        meta={"command": "case amount add", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_amount_list(args):
    from sud_cli.db import get_connection, init_db, list_case_amounts
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        amounts = list_case_amounts(conn, args.case_id)
    finally:
        conn.close()
    result(
        amounts,
        meta={"command": "case amount list", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


# refund 333.40 — норма уже VERIFIED (pack W4c), но применение возврата —
# решение по основанию через fee-refund движок; ручная маркировка обязательна.
_AMOUNT_REFUND_MR = (
    "manual_review: норма возврата 333.40 применена через fee-refund движок "
    "— сверь основание"
)

_AMOUNTS_DISCLAIMER = (
    "Расчёт справочный: реестр расходов — учётные записи оператора по делу, "
    "суммы сверяются с платёжными документами; sud не даёт фискальной гарантии."
)


def cmd_case_amount_set(args):
    from datetime import date as _date
    from sud_cli.db import get_connection, init_db, set_case_amount
    paid_at = args.paid_at
    if paid_at is not None:
        try:
            paid_at = _date.fromisoformat(paid_at).isoformat()
        except ValueError:
            return error(
                {"status": "INVALID_ARGUMENTS",
                 "detail": f"invalid paid_at {args.paid_at!r}: ожидается YYYY-MM-DD"},
                meta={"command": "case amount set", "state_dir": str(args.state_dir)},
                json_mode=args.json_mode,
            )
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        row = set_case_amount(conn, args.case_id, args.amount_id,
                              status=args.status, paid_at=paid_at)
    finally:
        conn.close()
    if row is None:
        return error(
            f"amount not found: {args.amount_id} for case {args.case_id}",
            meta={"command": "case amount set", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    payload = dict(row)
    if args.status == "refunded":
        # принимается, но маркируется: возврат по 333.40 требует сверки основания
        payload["warnings"] = [_AMOUNT_REFUND_MR]
    result(
        payload,
        meta={"command": "case amount set", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_amount_summary(args):
    from sud_cli.db import get_connection, init_db, summarize_case_amounts
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        summary = summarize_case_amounts(conn, args.case_id)
    finally:
        conn.close()
    summary["disclaimer"] = _AMOUNTS_DISCLAIMER
    result(
        summary,
        meta={"command": "case amount summary", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_reminders_due(args):
    """W4b §2 (S-26): окна сроков + bus-free a2a-строки.

    Здесь нет и не может быть запуска внешних процессов: модуль
    sud_cli.reminders чистый, строки только печатаются (as docx).
    """
    from datetime import date as _date, timedelta
    from sud_cli.db import get_connection, init_db, list_all_deadline_states
    from sud_cli.reminders import BUS_FREE_NOTE, due_items, generate_mail_commands

    days = args.days
    if not isinstance(days, int) or days < 1 or days > 90:
        return error(
            {"status": "INVALID_ARGUMENTS",
             "detail": f"--days должен быть целым 1..90, получен {days!r}"},
            meta={"command": "reminders due", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        states = list_all_deadline_states(conn)
    finally:
        conn.close()
    today = _date.today()
    items = due_items(states, today, days)
    payload = {
        "days": days,
        "today": today.isoformat(),
        "window_to": (today + timedelta(days=days)).isoformat(),
        "items": items,
        "commands": generate_mail_commands(items, days),
        "bus_free": BUS_FREE_NOTE,
    }
    result(
        payload,
        meta={"command": "reminders due", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_doc_add(args):
    from sud_cli.db import DocNotFoundError, add_case_document, get_connection, init_db
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        try:
            res = add_case_document(conn, args.case_id, args.path, title=args.title)
        except DocNotFoundError as exc:
            return error(
                {"status": "doc_not_found", "detail": str(exc)},
                meta={"command": "case doc add", "state_dir": str(args.state_dir)},
                json_mode=args.json_mode,
            )
    finally:
        conn.close()
    if res is None:
        return error(
            f"case not found: {args.case_id}",
            meta={"command": "case doc add", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        res,
        meta={"command": "case doc add", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_doc_list(args):
    from sud_cli.db import get_connection, init_db, list_case_documents
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        docs = list_case_documents(conn, args.case_id)
    finally:
        conn.close()
    result(
        docs,
        meta={"command": "case doc list", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_note_add(args):
    from sud_cli.db import add_case_note, get_connection, init_db
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        res = add_case_note(conn, args.case_id, args.text, author=args.author)
    finally:
        conn.close()
    if res is None:
        return error(
            f"case not found: {args.case_id}",
            meta={"command": "case note add", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        res,
        meta={"command": "case note add", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_note_list(args):
    from sud_cli.db import get_connection, init_db, list_case_notes
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        notes = list_case_notes(conn, args.case_id)
    finally:
        conn.close()
    result(
        notes,
        meta={"command": "case note list", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_note_supersede(args):
    from sud_cli.db import get_connection, init_db, supersede_case_note
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        try:
            note_id = int(args.note_id)
        except ValueError:
            return error(
                {"status": "INVALID_ARGUMENTS", "detail": "note_id must be int"},
                meta={"command": "case note supersede", "state_dir": str(args.state_dir)},
                json_mode=args.json_mode,
            )
        res = supersede_case_note(conn, note_id, args.text, author=args.author)
    finally:
        conn.close()
    if res is None:
        return error(
            "note not found",
            meta={"command": "case note supersede", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        res,
        meta={"command": "case note supersede", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_list(args):
    from sud_cli.db import get_connection, init_db, list_cases
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        cases = list_cases(
            conn, status=args.status, owner=args.owner, court=args.court, party=args.party
        )
    finally:
        conn.close()
    result(
        cases,
        meta={"command": "case list", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_case_card(args):
    from sud_cli.db import get_case_card, get_connection, init_db
    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        card = get_case_card(conn, args.case_id)
    finally:
        conn.close()
    if card is None:
        return error(
            f"case not found: {args.case_id}",
            meta={"command": "case card", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )
    result(
        card,
        meta={"command": "case card", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0

_CONFLICT_DISCLAIMER = (
    "Отсутствие совпадений не означает отсутствие конфликта: покрытие — "
    "только имена локального реестра; внешние стороны и аффилированные лица "
    "не охвачены. Решение остаётся за человеком (boundary IX-3)."
)


def _conflict_payload(hits):
    return {
        "clean": len(hits) == 0,
        "hits": hits,
        "method": "name_exact",
        "coverage": "local registry names only",
        "disclaimer": _CONFLICT_DISCLAIMER,
    }


def cmd_conflict_check(args):
    from sud_cli.conflict import check as conflict_check
    from sud_cli.db import get_connection, init_db, list_cases

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        portfolio = list_cases(conn)
        candidate = {
            "parties": [],
            "court": args.court,
            "case_number": args.case_number,
        }
        if args.plaintiff:
            candidate["parties"].append(args.plaintiff)
        if args.defendant:
            candidate["parties"].append(args.defendant)
        hits = conflict_check(portfolio, candidate)
    finally:
        conn.close()
    result(
        _conflict_payload(hits),
        meta={"command": "conflict check", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_conflict_case(args):
    from sud_cli.conflict import check as conflict_check
    from sud_cli.db import get_case, get_connection, init_db, list_cases

    init_db(args.state_dir)
    conn = get_connection(args.state_dir)
    try:
        case = get_case(conn, args.case_id)
        if case is None:
            return error(
                f"case not found: {args.case_id}",
                meta={"command": "conflict case", "state_dir": str(args.state_dir)},
                json_mode=args.json_mode,
            )
        portfolio = [c for c in list_cases(conn) if c.get("case_id") != args.case_id]
        candidate = {
            "parties": [],
            "case_id": args.case_id,
            "court": case.get("court"),
            "case_number": case.get("case_number"),
        }
        for key in ("plaintiff", "defendant"):
            val = case.get(key)
            if val:
                candidate["parties"].append(val)
        hits = conflict_check(portfolio, candidate)
    finally:
        conn.close()
    result(
        _conflict_payload(hits),
        meta={"command": "conflict case", "state_dir": str(args.state_dir)},
        json_mode=args.json_mode,
    )
    return 0


def cmd_jurisdiction_check(args):
    from sud_cli.jurisdiction import check_jurisdiction

    payload = check_jurisdiction(
        code=args.code,
        claim=args.claim,
        party_org=args.party_org,
        party_phys=args.party_phys,
        unknown_rf_location=args.unknown_rf_location,
        immovable=args.immovable,
        corporate=args.corporate,
        bankruptcy=args.bankruptcy,
        ip_rights=args.ip_rights,
        contract_forum=args.contract_forum,
        arbitration_clause=args.arbitration_clause,
        multiple_defendants=args.multiple_defendants,
        foreign_element=args.foreign_element,
        consumer=args.consumer,
        alimony=args.alimony,
    )
    result(payload, meta={"command": "jurisdiction check", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_pretrial_calc(args):
    from sud_cli.pretrial import calc_pretrial

    # S1-10: непарсимая дата раньше давала traceback вместо JSON-envelope.
    try:
        payload = calc_pretrial(code=args.code, sent=args.sent)
    except ValueError as exc:
        error(str(exc), meta={"command": "pretrial calc",
                            "state_dir": str(args.state_dir)},
              json_mode=args.json_mode)
        return 1
    result(payload, meta={"command": "pretrial calc", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_pretrial_consequences(args):
    from sud_cli.pretrial import consequences

    payload = consequences(code=args.code, stage=args.stage)
    result(payload, meta={"command": "pretrial consequences", "state_dir": str(args.state_dir)}, json_mode=args.json_mode)
    return 0


def cmd_update(args):
    from sud_cli.updater import check_update, run_update

    if not (getattr(args, "i_am_operator", False) or
            os.environ.get("SUD_CLI_ALLOW_UPDATE") == "1"):
        return error(
            "обновление — только оператором, из доверенного источника",
            meta={"command": "update", "state_dir": str(args.state_dir)},
            json_mode=args.json_mode,
        )

    payload = (
        check_update(source=args.source, target=args.target)
        if args.check
        else run_update(source=args.source, target=args.target)
    )
    failed = payload.get("status") == "error" or payload.get("ok") is False
    meta = {"command": "update", "state_dir": str(args.state_dir)}
    if failed:
        data = payload.get("data") or payload.get("error") or "UPDATE_FAILED"
        return error(data, meta=meta, json_mode=args.json_mode)
    result(payload, meta=meta, json_mode=args.json_mode)
    return 0


def cmd_draft(args):
    """Генерация процессуального документа (TASK_DRAFTER_INTEGRATION).

    Law Zero: недостающие обязательные реквизиты — структурированный отказ
    missing_required_fields с exit-кодом 2; никаких пустых полей.
    """
    from .drafter import draft_document, load_case_fields, templates_report

    import json as _json

    if getattr(args, "draft_command", None) == "templates":
        result(templates_report(), meta={"command": "draft templates"}, json_mode=args.json_mode)
        return 0

    if not args.doc_type:
        return error(
            {"status": "INVALID_ARGUMENTS",
             "detail": "укажите --type (см. `sud-cli draft templates`)"},
            meta={"command": "draft"},
            json_mode=args.json_mode,
        )
    try:
        fields = load_case_fields(args.case_id, args.case_file, args.state_dir)
    except (ValueError, OSError, _json.JSONDecodeError) as exc:
        return error(
            {"status": "INVALID_ARGUMENTS", "detail": str(exc)},
            meta={"command": "draft"},
            json_mode=args.json_mode,
        )
    flags = {
        "court": args.court,
        "plaintiff": args.plaintiff,
        "defendant": args.defendant,
        "claim_amount": args.claim_amount,
        "basis": args.basis,
        "petitum": args.petitum,
    }
    fields.update({k: v for k, v in flags.items() if v is not None})
    try:
        payload = draft_document(args.doc_type, out=args.out, doc_format=args.format, **fields)
    except (ValueError, TypeError) as exc:
        return error(
            {"status": "INVALID_ARGUMENTS", "detail": str(exc)},
            meta={"command": "draft"},
            json_mode=args.json_mode,
        )
    if payload.get("status") != "ok":
        # missing_required_fields — код 2 (Law Zero): сборка не выполнялась.
        return error(payload, meta={"command": "draft"}, json_mode=args.json_mode, code=2)
    result(payload, meta={"command": "draft"}, json_mode=args.json_mode)
    return 0
