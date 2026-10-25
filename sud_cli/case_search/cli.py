"""CLI for the synthetic core. No network, no real data, no cron, no Telegram.

Exit codes: 0 = ok, 2 = degraded (WAF/captcha/rate-limit), 3 = error
(empty/malformed/http/unavailable), 4 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._logging import get_logger, setup_logging
from .alerts import build_alert, write_alert
from .alerts_dedup import (
    alert_fingerprint,
    is_duplicate,
    load_seen_fingerprints,
    record_fingerprints,
)
from .config import load_watchlist
from .errors import CaseSearchError
from .pipeline import fetch, DEGRADED, ERROR, OK
from .snapshots import summary_line, watch_case
from .sources import REGISTRY, build_provider
from .sources.import_html import MosDomImportProvider
from .storage import SnapshotStore


def _default_runtime_url() -> str:
    """Browser mode for CLI: config camofox_url if set, else native embedded.

    ``CSE_CAMOFOX_URL=http://...`` opts into the external service; by default
    the package uses its own embedded Camoufox (no service required).
    """
    from .config import get_camofox_url

    return get_camofox_url() or "native"

_log = get_logger("cli")

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_ERROR = 3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="case-search",
        description="Legal court watcher — case tracker, judicial practice & full text extraction")
    # Global flags live on a shared parent of every subcommand, so they
    # are accepted AFTER the subcommand name natively (colleague-legal 1.3).
    # Flags BEFORE the subcommand are handled by _reorder_global_flags.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fixtures", default="fixtures/synthetic",
                        help="synthetic fixtures directory")
    common.add_argument("--store", default=".case-search/snapshots",
                        help="snapshot store directory")
    common.add_argument("--alerts", default=".case-search/alerts",
                        help="alert artifact directory")
    common.add_argument("--now", default=None,
                        help="fixed UTC timestamp for deterministic output")
    sub = parser.add_subparsers(dest="command", required=True)

    def _sub(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    lookup = _sub("lookup", help_text="single case lookup + snapshot")
    lookup.add_argument("case_ref")
    lookup.add_argument("--source", default=None,
                        choices=sorted(REGISTRY))
    lookup.add_argument("--json", dest="json_out", default=None,
                        help="write canonical snapshot JSON to this path")

    watch = _sub("watch", help_text="run the watchlist once")
    watch.add_argument("watchlist")
    watch.add_argument("--source", default=None,
                       choices=sorted(REGISTRY))
    watch.add_argument("--no-dedup", action="store_true", default=False,
                       help="disable alert deduplication")

    sub.add_parser("providers", help="show adapter implementation status",
                   parents=[common])

    doc = _sub("doctor", help_text="health-check the installation")
    doc.add_argument("--json", dest="doctor_json", action="store_true",
                     default=False, help="output pure JSON")
    doc.add_argument("--verbose", dest="doctor_verbose", action="store_true",
                     default=False, help="verbose diagnostics")

    imp = _sub("import",
               help_text="import a saved HTML page and parse it locally")
    imp.add_argument("html_file", help="path to saved HTML file")
    imp.add_argument("--case", required=True, dest="case_ref",
                     help="expected case number (identity check)")
    imp.add_argument("--json", dest="json_out", default=None,
                     help="write canonical snapshot JSON to this path")

    kadimp = _sub(
        "kad-import",
        help_text="import a saved KAD EXTRACT_EXPRESSION JSON and parse it")
    kadimp.add_argument("extract_json",
                        help="path to saved KAD extract JSON file")
    kadimp.add_argument("--case", required=True, dest="case_ref",
                        help="expected case number (identity check)")
    kadimp.add_argument("--json", dest="json_out", default=None,
                        help="write canonical snapshot JSON to this path")


    vsimp = _sub(
        "vsrf-import",
        help_text="import a saved vsrf.ru PDF document and parse it")
    vsimp.add_argument("pdf_file",
                       help="path to saved VS RF PDF document")
    vsimp.add_argument("--case", required=True, dest="case_ref",
                       help="expected case number (identity check)")
    vsimp.add_argument("--json", dest="json_out", default=None,
                       help="write canonical snapshot JSON to this path")

    vslookup = _sub(
        "vsrf-lookup",
        help_text="search the Supreme Court catalog (vkod.vsrf.ru)")
    vslookup.add_argument("query", help="search phrase")
    vslookup.add_argument("--json", dest="json_out", default=None,
                          help="write search results JSON to this path")

    autoimp = _sub(
        "auto-import",
        help_text="import any saved document (auto-detects the source)")
    autoimp.add_argument("html_file",
                         help="path to saved document (JSON/HTML/PDF)")
    autoimp.add_argument("--case", required=True, dest="case_ref",
                         help="expected case number (identity check)")
    autoimp.add_argument("--json", dest="json_out", default=None,
                         help="write canonical snapshot JSON to this path")

    srch = _sub(
        "search-practice",
        help_text="search sudact.ru practice by query (JSON results)")
    srch.add_argument("query", help="search phrase")
    srch.add_argument("--section", default="arbitral",
                      choices=["arbitral", "regular", "magistrate", "vsrf"],
                      help="sudact section (default: arbitral)")
    srch.add_argument("--page", type=int, default=1,
                      help="result page (default 1)")
    srch.add_argument("--limit", type=int, default=10,
                      help="max results to return (default 10, max 30)")
    srch.add_argument("--via-camofox", dest="via_camofox",
                      action="store_true", default=True,
                      help="use Camofox (127.0.0.1:9377) — REQUIRED for "
                           "real search results; direct HTTP gets only "
                           "a JS-required stub")
    srch.add_argument("--runtime-url", default=_default_runtime_url(),
                      help="Camofox runtime base URL (default: "
                           "http://127.0.0.1:9377)")
    srch.add_argument("--json", dest="json_out", default=None,
                      help="write search results JSON to this path")

    hist = _sub(
        "case-history",
        help_text="судьба дела: все акты по номеру + связанным инстанциям")
    hist.add_argument("case_number",
                      help="case number, e.g. А19-30387/2025")
    hist.add_argument("--section", default="arbitral",
                      choices=["arbitral", "regular", "magistrate", "vsrf"],
                      help="sudact section (default: arbitral)")
    hist.add_argument("--max-related", type=int, default=5,
                      help="max related instance numbers to follow (5)")
    hist.add_argument("--limit", type=int, default=10,
                      help="max docs per instance (default 10)")
    hist.add_argument("--runtime-url", default=_default_runtime_url(),
                      help="Camofox runtime base URL (default: "
                           "http://127.0.0.1:9377)")
    hist.add_argument("--json", dest="json_out", default=None,
                      help="write case history JSON to this path")

    sudimp = _sub(
        "sudrf-import",
        help_text="import a saved ГАС Правосудие (sudrf) HTML and parse it")
    sudimp.add_argument("html_file",
                        help="path to saved GAS document HTML (cp1251/utf-8)")
    sudimp.add_argument("--case", required=True, dest="case_ref",
                        help="expected case number (identity check)")
    sudimp.add_argument("--json", dest="json_out", default=None,
                        help="write canonical snapshot JSON to this path")

    exttext = _sub(
        "extract-text",
        help_text="extract full act text from a saved HTML/PDF")
    exttext.add_argument("file", help="path to saved document")
    exttext.add_argument("--out", dest="text_out", default=None,
                         help="write text to this path (default stdout)")

    gettext = _sub(
        "get-text",
        help_text="full decision text: sudact URL or local file (cached)")
    gettext.add_argument("url_or_file",
                         help="sudact.ru document URL or local file path")
    gettext.add_argument("--out", dest="text_out", default=None,
                         help="write text to this path")
    gettext.add_argument("--no-cache", dest="no_cache", action="store_true",
                         help="force refetch, ignore SQLite cache")
    gettext.add_argument("--blocks", dest="show_blocks", action="store_true",
                         help="print УСТАНОВИЛ/РЕШИЛ blocks separately")

    slive = _sub(
        "sudrf-live",
        help_text="live ГАС case card: номер → листинг → карточка "
                  "(plain HTTP, прокси)")
    slive.add_argument("case_number", help="case number, e.g. 33-35073/2026")
    slive.add_argument("--host", default="oblsud.mo.sudrf.ru",
                       help="sudrf court host (default oblsud.mo.sudrf.ru)")
    slive.add_argument("--date-window", type=int, default=5,
                       help="days to scan back in listings (default 5)")
    slive.add_argument("--proxy", default=None,
                       help="HTTP proxy (FIN residential; geo-block)")
    slive.add_argument("--json", dest="json_out", default=None,
                       help="write JSON to this path")
    slive.add_argument("--vnkod", default=None,
                       help="court code from catalog (e.g. 77RS0031) — overrides --host; works for ANY mapped court")
    slive.add_argument("--hearing-date", default=None,
                       help="known hearing date(s) DD.MM.YYYY, comma-"
                            "separated — for archived cases")

    # kad-case-history alias (SKILL.md использует это имя)
    hist_alias = _sub(
        "kad-case-history",
        help_text="alias for case-history (KAD Arbitr case history)")
    hist_alias.add_argument("case_number",
                            help="case number, e.g. А19-30387/2025")
    hist_alias.add_argument("--section", default="arbitral",
                            choices=["arbitral", "regular", "magistrate",
                                     "vsrf"])
    hist_alias.add_argument("--limit", type=int, default=10,
                            help="max docs per instance (default 10)")
    hist_alias.add_argument("--max-related", type=int, default=5,
                            help="max related instance numbers to follow (default 5)")
    hist_alias.add_argument("--runtime-url", default=_default_runtime_url(),
                            help="Camofox runtime base URL (default: "
                                 "http://127.0.0.1:9377)")
    hist_alias.add_argument("--json", dest="json_out", default=None,
                            help="write case history JSON to this path")

    kadsearch = _sub(
        "kad-search",
        help_text="search KAD cases by participant (INN / OGRN / name)")
    kadsearch.add_argument("--inn", default=None,
                           help="participant INN")
    kadsearch.add_argument("--ogrn", default=None,
                           help="participant OGRN / OGRNIP")
    kadsearch.add_argument("--name", default=None,
                           help="participant name / company name")
    kadsearch.add_argument("--page", type=int, default=1,
                           help="result page (default 1)")
    kadsearch.add_argument("--runtime-url",
                           default=_default_runtime_url(),
                           help="Camofox runtime base URL (default: "
                                "http://127.0.0.1:9377)")
    kadsearch.add_argument("--json", dest="json_out", default=None,
                           help="write search results JSON to this path")

    slk = _sub(
        "sudrf-lookup",
        help_text="каталог судов ГАС: поиск по названию/типу/vnkod")
    slk.add_argument("--name", default="",
                     help="подстрока названия суда")
    slk.add_argument("--vnkod", default="",
                     help="точный код суда (e.g. 50RS0001)")
    slk.add_argument("--type", default="",
                     help="тип: RS/OS/GV/UD")
    slk.add_argument("--limit", type=int, default=20,
                     help="max results (default 20)")
    slk.add_argument("--json", dest="json_out", default=None,
                     help="write JSON to this path")

    mlive = _sub(
        "mos-live",
        help_text="live карточка дела: суды Москвы (mos-gorsud.ru)")
    mlive.add_argument("court_slug",
                       help="court slug, e.g. horoshevskij")
    mlive.add_argument("case_number", help="e.g. 02-2101/2026")
    mlive.add_argument("--kind", default=None,
                       help="case kind: civil|kao|adm|criminal")
    mlive.add_argument("--json", dest="json_out", default=None,
                       help="write JSON to this path")

    mresolve = _sub(
        "mos-resolve",
        help_text="resolve Moscow court name/slug to canonical mos-gorsud slug")
    mresolve.add_argument("name_or_slug",
                          help="court name or slug, e.g. 'Тверской' or tverskoj")

    cfg = _sub(
        "config",
        help_text="настройка инструмента (proxy/camofox/license)")
    cfg.add_argument("action", choices=["set", "show"])
    cfg.add_argument("key", nargs="?", default=None)
    cfg.add_argument("value", nargs="?", default=None)
    return parser


# Global flags accepted both before and after the subcommand.
GLOBAL_FLAG_NAMES = ("--fixtures", "--store", "--alerts", "--now")


def _reorder_global_flags(argv: list[str]) -> list[str]:
    """Move leading global flags (with values) to the END of argv so a
    single parents=[common] registration covers both positions."""
    flags_before: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in GLOBAL_FLAG_NAMES:
            flags_before.append(token)
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                flags_before.append(argv[i + 1])
                i += 2
            else:
                i += 1
        else:
            rest.append(token)
            i += 1
    return rest + flags_before


def _status_exit(status: str) -> int:
    return {"ok": EXIT_OK, "degraded": EXIT_DEGRADED}.get(
        status, EXIT_ERROR)


def _agent_envelope(outcome, provider=None) -> dict:
    """Agent-facing JSON: canonical snapshot + optional extensions
    (instances/events raw data the schema doesn't carry)."""
    envelope: dict = {"snapshot": outcome.snapshot.to_dict()
                      if outcome.snapshot else None}
    extensions: dict = {}
    instances = getattr(provider, "last_instances", None)
    if instances:
        extensions["instances"] = instances
    events_raw = getattr(provider, "last_events", None)
    if events_raw:
        extensions["events_raw"] = events_raw
    if extensions:
        envelope["extensions"] = extensions
    return envelope


def _write_agent_json(args: argparse.Namespace, outcome,
                      provider=None) -> None:
    if not getattr(args, "json_out", None) or not outcome.snapshot:
        return
    envelope = _agent_envelope(outcome, provider)
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    Path(args.json_out).write_text(
        _json.dumps(envelope, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")


def _run_case(
    args: argparse.Namespace,
    case_ref: str,
    source: str | None,
    *,
    dedup_store: Path | None = None,
) -> tuple[int, "Alert | None", "str | None", bool]:
    """Shared lookup/watch logic for one case.

    Returns ``(exit_code, alert, fingerprint, emitted)``. When
    ``dedup_store`` is provided and the alert is a duplicate it is not
    written and ``emitted`` is False.
    """
    provider = build_provider(source or "synthetic", args.fixtures)
    store = SnapshotStore(args.store)
    outcome = fetch(provider, case_ref, args.now or _utc_now_iso())
    result = watch_case(store, outcome)

    print(f"status: {outcome.status} ({outcome.reason})")
    if result.stored:
        print(f"snapshot: {result.stored.path}")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        json_out = getattr(args, "json_out", None)
        if json_out:
            Path(json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(json_out).write_text(outcome.snapshot.to_json() + "\n",
                                      encoding="utf-8")
            print(f"json artifact: {json_out}")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    for change in result.changes:
        print(f"change: {change.path}: {change.before!r} -> "
              f"{change.after!r}")

    alert = build_alert(case_ref, result, now=args.now)
    fingerprint: str | None = None
    emitted = False
    if alert:
        fingerprint = alert_fingerprint(case_ref, alert)
        if dedup_store is not None and is_duplicate(dedup_store, fingerprint):
            print(f"alert: duplicate suppressed ({fingerprint})")
        else:
            print(f"alert: {write_alert(alert, args.alerts)}")
            emitted = True
    return _status_exit(outcome.status), alert, fingerprint, emitted


def cmd_lookup(args: argparse.Namespace) -> int:
    code, _, _, _ = _run_case(args, args.case_ref, args.source)
    return code


def cmd_import(args: argparse.Namespace) -> int:
    from .mos.parser import fetch_mos_html
    provider = MosDomImportProvider(args.html_file, args.case_ref)
    outcome = fetch_mos_html(provider, args.case_ref,
                             args.now or _utc_now_iso())
    store = SnapshotStore(args.store)
    result = watch_case(store, outcome)
    print(f"status: {outcome.status} ({outcome.reason})")
    if result.stored:
        print(f"snapshot: {result.stored.path}")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(outcome.snapshot.to_json() + "\n",
                                           encoding="utf-8")
            print(f"json artifact: {args.json_out}")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    alert = build_alert(args.case_ref, result, now=args.now)
    if alert:
        print(f"alert: {write_alert(alert, args.alerts)}")
    for change in result.changes:
        print(f"change: {change.path}: {change.before!r} -> "
              f"{change.after!r}")
    return _status_exit(outcome.status)


def cmd_kad_import(args: argparse.Namespace) -> int:
    from .kad.import_json import KadImportProvider
    from .kad.parser import fetch_kad_html
    provider = KadImportProvider(args.extract_json, args.case_ref)
    outcome = fetch_kad_html(provider, args.case_ref,
                             args.now or _utc_now_iso())
    _write_agent_json(args, outcome, provider)
    store = SnapshotStore(args.store)
    result = watch_case(store, outcome)
    print(f"status: {outcome.status} ({outcome.reason})")
    if result.stored:
        print(f"snapshot: {result.stored.path}")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(outcome.snapshot.to_json() + "\n",
                                           encoding="utf-8")
            print(f"json artifact: {args.json_out}")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    alert = build_alert(args.case_ref, result, now=args.now)
    if alert:
        print(f"alert: {write_alert(alert, args.alerts)}")
    for change in result.changes:
        print(f"change: {change.path}: {change.before!r} -> "
              f"{change.after!r}")
    return _status_exit(outcome.status)


def cmd_sudact_import(args: argparse.Namespace) -> int:
    from .sudact.parser import SudactImportProvider, fetch_sudact_html
    provider = SudactImportProvider(args.html_file, args.case_ref)
    outcome = fetch_sudact_html(provider, args.case_ref,
                                args.now or _utc_now_iso())
    store = SnapshotStore(args.store)
    result = watch_case(store, outcome)
    print(f"status: {outcome.status} ({outcome.reason})")
    if result.stored:
        print(f"snapshot: {result.stored.path}")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(outcome.snapshot.to_json() + "\n",
                                           encoding="utf-8")
            print(f"json artifact: {args.json_out}")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    alert = build_alert(args.case_ref, result, now=args.now)
    if alert:
        print(f"alert: {write_alert(alert, args.alerts)}")
    for change in result.changes:
        print(f"change: {change.path}: {change.before!r} -> "
              f"{change.after!r}")
    return _status_exit(outcome.status)


def cmd_vsrf_import(args: argparse.Namespace) -> int:
    from .vsrf.parser import VsrfImportProvider, fetch_vsrf_pdf
    provider = VsrfImportProvider(args.pdf_file, args.case_ref)
    outcome = fetch_vsrf_pdf(provider, args.case_ref,
                             args.now or _utc_now_iso())
    store = SnapshotStore(args.store)
    result = watch_case(store, outcome)
    print(f"status: {outcome.status} ({outcome.reason})")
    if result.stored:
        print(f"snapshot: {result.stored.path}")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(outcome.snapshot.to_json() + "\n",
                                           encoding="utf-8")
            print(f"json artifact: {args.json_out}")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    alert = build_alert(args.case_ref, result, now=args.now)
    if alert:
        print(f"alert: {write_alert(alert, args.alerts)}")
    for change in result.changes:
        print(f"change: {change.path}: {change.before!r} -> "
              f"{change.after!r}")
    return _status_exit(outcome.status)


def cmd_vsrf_lookup(args: argparse.Namespace) -> int:
    import json as _json
    from .vsrf.live import VsrfAuthError, VsrfClient, VsrfLiveError

    try:
        client = VsrfClient()
        client.login()
        results = client.search_catalog(args.query)
    except VsrfAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except VsrfLiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"found: {len(results)}")
    for r in results[:20]:
        case_number = r.get("case_number") or "?"
        title = (r.get("title") or "-")[:60]
        print(f"  {case_number} | {title}")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(results, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def _detect_source(path: Path) -> str:
    """Auto-detect import source by file shape (colleague-legal feedback 1.1).

    - .pdf                          -> vsrf-import
    - JSON object with case_number  -> kad-import
    - HTML with sudact markers      -> sudact-import
    - HTML with mos-gorsud markers  -> mos-gorsud-import (import)
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "vsrf"
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError as exc:
        raise ValueError(f"cannot read import file: {exc}") from exc
    stripped = head.lstrip()
    if stripped.startswith("{"):
        import json
        try:
            data = json.loads(head)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, (dict, str)):
            return "kad"
    lowered = head.lower()
    if "js-case-header-case_num" in head:
        return "kad-html"
    if "h-col1-inner3" in head or "судакт" in lowered:
        return "sudact"
    if "anti-container" in lowered or "mos-gorsud" in lowered \
            or "моспортал" in lowered:
        return "mos"
    raise ValueError(
        f"cannot auto-detect source for '{path.name}': no known markers; "
        f"use an explicit subcommand (kad-import/sudact-import/"
        f"vsrf-import/import)")


def cmd_import_auto(args: argparse.Namespace) -> int:
    from .kad.import_json import KadImportProvider
    from .kad.parser import fetch_kad_html
    from .mos.parser import fetch_mos_html
    from .sources.import_html import MosDomImportProvider
    from .sudact.parser import SudactImportProvider, fetch_sudact_html
    from .vsrf.parser import VsrfImportProvider, fetch_vsrf_pdf

    path = Path(args.html_file)
    source = _detect_source(path)
    provider = None
    if source == "kad":
        provider = KadImportProvider(path, args.case_ref)
        outcome = fetch_kad_html(provider, args.case_ref,
                                 args.now or _utc_now_iso())
    elif source == "kad-html":
        from .kad.html_import import (KadHtmlImportProvider,
                                      fetch_kad_saved_html)
        provider = KadHtmlImportProvider(path, args.case_ref)
        outcome = fetch_kad_saved_html(provider, args.case_ref,
                                       args.now or _utc_now_iso())
    elif source == "sudact":
        provider = SudactImportProvider(path, args.case_ref)
        outcome = fetch_sudact_html(provider, args.case_ref,
                                    args.now or _utc_now_iso())
    elif source == "vsrf":
        provider = VsrfImportProvider(path, args.case_ref)
        outcome = fetch_vsrf_pdf(provider, args.case_ref,
                                 args.now or _utc_now_iso())
    else:
        provider = MosDomImportProvider(path, args.case_ref)
        outcome = fetch_mos_html(provider, args.case_ref,
                                 args.now or _utc_now_iso())

    store = SnapshotStore(args.store)
    result = watch_case(store, outcome)
    print(f"source: {source}")
    print(f"status: {outcome.status} ({outcome.reason})")
    if result.stored:
        print(f"snapshot: {result.stored.path}")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        if args.json_out:
            _write_agent_json(args, outcome, provider)
            print(f"json artifact: {args.json_out}")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    alert = build_alert(args.case_ref, result, now=args.now)
    if alert:
        print(f"alert: {write_alert(alert, args.alerts)}")
    for change in result.changes:
        print(f"change: {change.path}: {change.before!r} -> "
              f"{change.after!r}")
    return _status_exit(outcome.status)


def _probe_runtime_or_exit(runtime_url: str) -> int | None:
    """Probe the Camofox runtime; return EXIT_DEGRADED (2) if down.

    ``runtime_url == "native"`` (the default) skips the external-service
    probe — the embedded browser launches lazily on first use.
    Prints an actionable stderr message and returns an exit code.  None
    means the runtime is reachable and the caller may proceed.
    """
    if runtime_url == "native":
        return None

    from .sudact.runtime import probe_browser_runtime

    probe = probe_browser_runtime(runtime_url, timeout=5.0)
    if not probe["ok"]:
        print(
            f"browser runtime not available at {runtime_url}: "
            f"{probe['error']}. Start the camofox runtime service, drop "
            "--runtime-url to use the embedded browser, or pass "
            "--runtime-url native.",
            file=sys.stderr,
        )
        print("hint: python3 -m case_search doctor | grep runtime",
              file=sys.stderr)
        return EXIT_DEGRADED
    return None


def _handle_runtime_error(exc: Exception) -> int:
    """Print an actionable runtime error + doctor hint and return exit 2."""
    print(f"error: {exc}", file=sys.stderr)
    print("hint: python3 -m case_search doctor | grep runtime",
          file=sys.stderr)
    if "browser died" in str(exc) or "HTTP 500" in str(exc):
        print(
            "hint: the Camofox browser process appears dead — restart the "
            "runtime service and retry the same command (transient state).",
            file=sys.stderr)
    return EXIT_DEGRADED


def cmd_search_practice(args: argparse.Namespace) -> int:
    import json as _json
    from .sudact.search import search
    from .sudact.runtime import js_fetcher
    from .errors import CaseSearchError, SudactRuntimeError

    if args.via_camofox:
        probe_code = _probe_runtime_or_exit(args.runtime_url)
        if probe_code is not None:
            return probe_code
        fetcher = js_fetcher(args.runtime_url, timeout=90.0)
    else:
        import urllib.request as _ureq

        def fetcher(url: str) -> str:
            from sud_cli.sud_netguard import breaker_trip, check_before_fetch
            check_before_fetch(url)  # netguard: fail-closed egress+breaker
            req = _ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with _ureq.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            except _ureq.HTTPError as exc:
                if exc.code in (403, 429):
                    breaker_trip(url, reason=f"http_{exc.code}")
                raise
            return body

    try:
        result = search(args.query, fetcher,
                        section=args.section, page=args.page,
                        limit=args.limit)
    except SudactRuntimeError as exc:
        return _handle_runtime_error(exc)
    except CaseSearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    d = result.to_dict()
    print(_json.dumps(d, ensure_ascii=False, indent=1))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(d, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def cmd_case_history(args: argparse.Namespace) -> int:
    import json as _json
    from .errors import CaseSearchError, SudactRuntimeError
    from .sudact.runtime import js_fetcher
    from .sudact.case_history import search_case_history

    probe_code = _probe_runtime_or_exit(args.runtime_url)
    if probe_code is not None:
        return probe_code

    try:
        fetcher = js_fetcher(args.runtime_url, timeout=90.0)
        result = search_case_history(
            args.case_number, fetcher, section=args.section,
            max_related=args.max_related, limit=args.limit)
    except SudactRuntimeError as exc:
        return _handle_runtime_error(exc)
    except CaseSearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if result.get("total_documents", 0) == 0:
        print(f"error: {result.get('error', 'empty')}", file=sys.stderr)
        return EXIT_ERROR

    print(_json.dumps(result, ensure_ascii=False, indent=1))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(result, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def cmd_kad_case_history(args: argparse.Namespace) -> int:
    import json as _json
    from .public_api import kad_case_history

    probe_code = _probe_runtime_or_exit(args.runtime_url)
    if probe_code is not None:
        return probe_code

    out = kad_case_history(args.case_number, camofox_base=args.runtime_url)
    if not out.ok:
        print(f"error: {out.reason}", file=sys.stderr)
        return EXIT_ERROR if out.status == "error" else EXIT_DEGRADED
    d = out.to_dict()
    print(_json.dumps(d.get("extensions", d), ensure_ascii=False, indent=1))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(d, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def cmd_kad_search(args: argparse.Namespace) -> int:
    import json as _json
    from .public_api import kad_search

    out = kad_search(
        inn=args.inn, ogrn=args.ogrn, name=args.name, page=args.page,
        camofox_base=args.runtime_url)
    if not out.ok:
        print(f"error: {out.reason}", file=sys.stderr)
        return EXIT_ERROR if out.status == "error" else EXIT_DEGRADED
    ext = out.extensions
    hits = ext.get("hits", [])
    print(f"found: {len(hits)}")
    for h in hits[:20]:
        parties = ", ".join(h.get("parties", []))
        print(f"  {h.get('case_number')} | {h.get('court')} | "
              f"{h.get('judge')} | {parties}")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(out.to_dict(), ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"json artifact: {args.json_out}")
    return EXIT_OK


def cmd_sudrf_import(args: argparse.Namespace) -> int:
    import json as _json
    from .errors import CaseSearchError
    from .sudrf.parser import SudrfImportProvider, fetch_sudrf_html
    provider = SudrfImportProvider(args.html_file, args.case_ref)
    try:
        outcome = fetch_sudrf_html(provider, args.case_ref,
                                   args.now or _utc_now_iso())
    except CaseSearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    store = SnapshotStore(args.store)
    result = watch_case(store, outcome)
    print(f"status: {outcome.status} ({outcome.reason})")
    if outcome.snapshot:
        print(summary_line(outcome.snapshot))
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(
                outcome.snapshot.to_json() + "\n", encoding="utf-8")
    else:
        print("snapshot: none fabricated (previous baseline preserved)")
    return _status_exit(outcome.status)


def cmd_extract_text(args: argparse.Namespace) -> int:
    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"error: file not found: {file_path}", file=sys.stderr)
        return EXIT_ERROR
    if file_path.suffix.lower() == ".pdf":
        from .vsrf.parser import extract_pdf_text
        try:
            text = extract_pdf_text(file_path)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
    else:
        from .sudrf.parser import extract_full_text
        raw = file_path.read_bytes()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("cp1251", errors="replace")
        text = extract_full_text(html)
    if not text.strip():
        print("error: no text extracted (scan? use OCR)", file=sys.stderr)
        return EXIT_ERROR
    if args.text_out:
        Path(args.text_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.text_out).write_text(text, encoding="utf-8")
        print(f"text written: {args.text_out} ({len(text)} chars)")
    else:
        print(text)
    return EXIT_OK


def cmd_get_text(args: argparse.Namespace) -> int:
    import json as _json
    from .public_api import get_decision_text
    out = get_decision_text(args.url_or_file,
                            use_cache=not args.no_cache)
    if not out.ok:
        print(f"error: {out.reason}", file=sys.stderr)
        return EXIT_ERROR if out.status == "error" else EXIT_DEGRADED
    ext = out.extensions
    if args.show_blocks and ext.get("blocks"):
        for name, txt in ext["blocks"].items():
            print(f"===== {name.upper()} =====")
            print(txt)
    elif args.text_out:
        Path(args.text_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.text_out).write_text(ext["full_text"], encoding="utf-8")
        print(f"text written: {args.text_out} ({ext['full_text_chars']} chars)")
    else:
        print(ext["full_text"])
    return EXIT_OK


def cmd_sudrf_live(args: argparse.Namespace) -> int:
    import json as _json
    from .public_api import get_sudrf_case, get_sudrf_case_by_vnkod
    dates = ([d.strip() for d in args.hearing_date.split(",")]
             if getattr(args, "hearing_date", None) else None)
    if getattr(args, "vnkod", None):
        out = get_sudrf_case_by_vnkod(args.vnkod, args.case_number,
                                      date_window=args.date_window,
                                      explicit_dates=dates,
                                      proxy=args.proxy)
    else:
        out = get_sudrf_case(args.case_number, host=args.host,
                             date_window=args.date_window, proxy=args.proxy)
    if not out.ok:
        print(f"error: {out.reason}", file=sys.stderr)
        return EXIT_ERROR if out.status == "error" else EXIT_DEGRADED
    ext = out.extensions
    print(f"case: {ext['case_number']} | uid: {ext['uid']}")
    hdr = ext.get("header", {})
    print(f"category: {hdr.get('Категория дела', '-')}")
    print(f"judge: {hdr.get('Судья', '-')}")
    print(f"motion events: {len(ext.get('motion', []))}")
    for e in ext.get("motion", []):
        print(f"  {e.get('date')} {e.get('time')} | {e.get('event')}")
    parts = ext.get("participants", [])
    masked = sum(1 for x in parts if x.get("masked") == "true")
    print(f"participants: {len(parts)} (masked: {masked})")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(out.to_dict(), ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def cmd_sudrf_lookup(args: argparse.Namespace) -> int:
    import json as _json
    from .sudrf.live import catalog_search
    hits = catalog_search(pattern=args.name, vnkod=args.vnkod,
                          court_type=args.type)
    print(f"found: {len(hits)}")
    for h in hits[:args.limit]:
        print(f"  {h['vnkod']} | {h['name'][:60]}")
        print(f"         {h['adress'][:70]}")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(hits, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    from . import config as _cfg
    if args.action == "show":
        for k, v in _cfg.as_dict().items():
            shown = v if k != "license_key" else (
                v[:12] + "..." if v else "(not set)")
            print(f"{k:20} = {shown or '(default)'}")
        return EXIT_OK
    if args.action == "set":
        if not args.key or args.value is None:
            print("usage: case-search config set <key> <value>",
                  file=sys.stderr)
            return 4
        _cfg.set_value(args.key, args.value)
        print(f"saved: {args.key}")
        return EXIT_OK
    return 4


def cmd_mos_live(args: argparse.Namespace) -> int:
    import json as _json
    from .public_api import get_mos_case
    out = get_mos_case(args.court_slug, args.case_number, kind=args.kind)
    if not out.ok:
        print(f"error: {out.reason}", file=sys.stderr)
        return EXIT_ERROR if out.status == "error" else EXIT_DEGRADED
    ext = out.extensions
    print(f"court: {args.court_slug} | judge: {ext.get('judge', '-')}")
    print(f"status: {ext.get('current_status', '-')}")
    for x in ext.get("parties", []):
        print(f"  {x['role']}: {x['name']}")
    print(f"motion events: {len(ext.get('motion', []))}")
    for e in ext.get("motion", []):
        print(f"  {e['date']} | {e['event']}")
    print(f"documents: {len(ext.get('documents', []))}")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            _json.dumps(out.to_dict(), ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return EXIT_OK


def cmd_mos_resolve(args: argparse.Namespace) -> int:
    from .mos.live import resolve_mos_slug, KNOWN_COURTS
    slug = resolve_mos_slug(args.name_or_slug)
    if slug is None:
        print("not found", file=sys.stderr)
        return 1
    print(f"{slug} | {KNOWN_COURTS[slug]}")
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    worst = EXIT_OK
    emitted_count = 0
    dup_count = 0
    emitted_fps: dict[str, str] = {}
    dedup_store: Path | None = None
    if not args.no_dedup:
        dedup_store = Path(args.alerts) / "seen_fingerprints.json"

    for entry in load_watchlist(args.watchlist):
        source = args.source or entry.source
        code, _alert, fp, emitted = _run_case(
            args, entry.case_ref, source, dedup_store=dedup_store)
        worst = max(worst, code)
        if emitted and fp is not None:
            emitted_count += 1
            emitted_fps[fp] = args.now or _utc_now_iso()
        elif _alert is not None and fp is not None and dedup_store is not None:
            dup_count += 1

    if dedup_store is not None:
        existing = load_seen_fingerprints(dedup_store)
        existing.update(emitted_fps)
        record_fingerprints(dedup_store, existing)

    print(f"watch: {emitted_count} alerts, {dup_count} duplicates "
          f"suppressed; exit: {worst}")
    return worst


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor as _doctor
    return _doctor.main_doctor(
        json_out=args.doctor_json, verbose=args.doctor_verbose)


def cmd_providers(_: argparse.Namespace) -> int:
    table = {
        "synthetic": "IMPLEMENTED (fixtures only, no network)",
        "kad": ("IMPLEMENTED LIVE (kad-search by participant via Camofox UI; "
                "kad-case-history full KAD traversal; import for saved "
                "JSON/HTML; captcha -> DEGRADED)"),
        "kad-case-history": ("IMPLEMENTED LIVE (full KAD UI traversal via "
                             "Camofox; not a SudAct alias since 2026-09-19)"),
        "mos-gorsud": ("IMPLEMENTED LIVE (mos-live / get_mos_case over plain "
                       "HTTPS, no browser; 58 courts in KNOWN_COURTS)"),
        "mos-gorsud-dom": ("IMPLEMENTED (replay-only: saved redacted "
                           "fixtures, no network)"),
        "mos-gorsud-import": "IMPLEMENTED (local HTML import, no network)",
        "sudact": ("IMPLEMENTED LIVE (search-practice / case-history / "
                   "get-text via Camofox JS runtime; captcha -> DEGRADED; "
                   "fresh 2026 cases may lag in SudAct indexing)"),
        "sudact-import": "IMPLEMENTED (local HTML import, no network)",
        "sudrf": ("IMPLEMENTED LIVE (sudrf-live / get_sudrf_case_by_vnkod "
                  "over plain HTTP via proxy, date-window listing scan; "
                  "2335-court catalog in registry.json)"),
        "vsrf": ("SCAFFOLD (catalog requires CSE_VSRF_USERNAME/PASSWORD; "
                 "not verified live; vsrf-import of saved PDFs works)"),
        "vsrf-import": "IMPLEMENTED (local PDF import via pymupdf, no network)",
    }
    print(json.dumps(table, sort_keys=True, indent=2))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    if argv is None:
        import sys as _sys
        argv = _sys.argv[1:]
    args = parser.parse_args(_reorder_global_flags(list(argv)))

    # License gate: live online commands only (offline import free)
    LIVE_COMMANDS = {"search-practice", "sudrf-live"}
    if getattr(args, "command", None) in LIVE_COMMANDS:
        from .license import load_license, LicenseError
        try:
            load_license()
        except LicenseError as exc:
            print(f"license error: {exc}", file=sys.stderr)
            return 4
    handlers = {"lookup": cmd_lookup, "watch": cmd_watch,
                "providers": cmd_providers, "doctor": cmd_doctor,
                "import": cmd_import,
                "kad-import": cmd_kad_import,
                "sudact-import": cmd_sudact_import,
                "vsrf-import": cmd_vsrf_import,
                "vsrf-lookup": cmd_vsrf_lookup,
                "auto-import": cmd_import_auto,
                "search-practice": cmd_search_practice,
                "case-history": cmd_case_history,
                "kad-case-history": cmd_kad_case_history,
                "kad-search": cmd_kad_search,
                "sudrf-import": cmd_sudrf_import,
                "extract-text": cmd_extract_text,
                "get-text": cmd_get_text,
                "sudrf-live": cmd_sudrf_live,
                "sudrf-lookup": cmd_sudrf_lookup,
                "config": cmd_config,
                "mos-live": cmd_mos_live,
                "mos-resolve": cmd_mos_resolve}
    _log.debug("command=%s", args.command)
    try:
        return handlers[args.command](args)
    except (CaseSearchError, OSError, ValueError) as exc:
        _log.error("%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
