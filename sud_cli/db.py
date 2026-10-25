"""Isolated SQLite persistence layer for sud-cli."""

import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = "3"
DB_NAME = "store.db"


class SchemaError(Exception):
    """Raised when the database schema version is unsupported."""


# Phase 1 (schema v1) -> Phase 3 (schema v2).
# Executed atomically inside a single explicit transaction. If any statement
# fails, the upgrade is rolled back and the database remains at its previous
# version. Schema objects for v2 are created before this migration runs, so the
# tuple only performs data movement and the final version bump.
_V1_TO_V2_MIGRATION = (
    """
    INSERT OR IGNORE INTO cases
        (case_id, case_number, court, plaintiff, defendant,
         source_name, source_record_id, fetched_at, source_url, imported_at)
    SELECT
        case_number, case_number, court, plaintiff, defendant,
        'local-json-court-migrated', record_id, imported_at, NULL, imported_at
    FROM records;
    """,
    """
    INSERT OR IGNORE INTO events
        (event_id, case_id, event_date, event_type, doc_text,
         source_name, source_record_id, fetched_at, source_url, imported_at)
    SELECT
        record_id, case_number, event_date, event_type, doc_text,
        'local-json-court-migrated', record_id, imported_at, NULL, imported_at
    FROM records;
    """,
    "UPDATE meta SET value = '2' WHERE key = 'schema_version';",
)


def _ensure_case_documents_columns(conn):
    """the wave §2: идемпотентная миграция case_documents -> +supersedes.

    Как the wave для case_amounts: ALTER ADD COLUMN с NULL по умолчанию, старые
    строки остаются supersedes=NULL — поведение существующих записей не меняется.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(case_documents)").fetchall()}
    if not cols:
        return  # таблицы ещё нет (fresh DB создаётся сразу с новой колонкой)
    if "supersedes" not in cols:
        conn.execute("ALTER TABLE case_documents ADD COLUMN supersedes INTEGER")


def _ensure_evidence_items_table(conn):
    """the wave §1 (S-73): идемпотентное создание evidence_items без bump схемы.

    SCHEMA_VERSION остаётся '3': таблица добавляется тем же ensure-шагом,
    каким the wave добавлял колонки case_amounts — старые пины версии не плывут.
    media — СВОБОДНЫЙ TEXT с проверкой значения на входе (валидация аргумента,
    не enum в схеме).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            volume TEXT,
            sheet TEXT,
            doc_ref INTEGER REFERENCES case_documents(id),
            sha256 TEXT,
            media TEXT,
            added_at TEXT NOT NULL,
            note TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_items_case ON evidence_items(case_id);"
    )


def _ensure_case_representatives_table(conn):
    """the wave §2 (S-15): идемпотентное создание case_representatives без bump схемы.

    Тот же ensure-паттерн, что the wave (case_amounts) и the wave (evidence_items):
    SCHEMA_VERSION остаётся '3'. side — СВОБОДНЫЙ TEXT в схеме, проверяться
    будет на входе (валидация аргумента, не выдуманный enum в DDL);
    power_docs — свободный текст описания доверенности: sud не реестр
    полномочий и автоматическую проверку не делает.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_representatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            side TEXT NOT NULL,
            name TEXT NOT NULL,
            authority_note TEXT,
            power_docs TEXT,
            added_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_case_reps_case ON case_representatives(case_id);"
    )


def _ensure_cases_columns(conn):
    """the wave §1 (S-12): идемпотентная колонка cases.instance (без bump схемы).

    Цепочка инстанций (§1) берёт instance из поля дела, как и матрица the wave
    (`case.get('instance')`). В схеме W1 такой колонки не было — add-only,
    старые строки остаются NULL (= unknown), поведение не меняется.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
    if not cols:
        return  # таблицы ещё нет (fresh DB создаётся сразу с новой колонкой)
    if "instance" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN instance TEXT")


def _ensure_case_amounts_columns(conn):
    """the wave §1: идемпотентная миграция case_amounts -> +status/+paid_at.

    kind остаётся СВОБОДНЫЙ TEXT (никаких выдуманных enum в схеме).
    ALTER ADD COLUMN с константным DEFAULT заполняет старые строки
    значением 'planned' и не трогает остальные колонки.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(case_amounts)").fetchall()}
    if not cols:
        return  # таблицы ещё нет (fresh DB создаётся сразу с новыми колонками)
    if "status" not in cols:
        conn.execute("ALTER TABLE case_amounts ADD COLUMN status TEXT DEFAULT 'planned'")
    if "paid_at" not in cols:
        conn.execute("ALTER TABLE case_amounts ADD COLUMN paid_at TEXT")


def _v2_to_v3_migration(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
    if "status" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN status TEXT;")
    if "owner" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN owner TEXT;")
    if "closed_at" not in cols:
        conn.execute("ALTER TABLE cases ADD COLUMN closed_at TEXT;")
    _ensure_case_amounts_columns(conn)
    # the wave: те же идемпотентные ensure-шаги в миграции v2->v3 (свежая база
    # получает их напрямую из init_db).
    _ensure_evidence_items_table(conn)
    _ensure_case_documents_columns(conn)
    # the wave §1+§2: case_representatives (новая таблица) и cases.instance
    # (новая колонка) — тем же ensure-паттерном, без bump SCHEMA_VERSION.
    _ensure_case_representatives_table(conn)
    _ensure_cases_columns(conn)
    conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version';")


_V2_TO_V3_MIGRATION = (_v2_to_v3_migration, _ensure_case_amounts_columns,
                       _ensure_evidence_items_table, _ensure_case_documents_columns,
                       _ensure_case_representatives_table, _ensure_cases_columns)


def _db_path(state_dir: Path) -> Path:
    return state_dir / DB_NAME


def _get_schema_version(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        # meta table may not exist yet on a completely empty database.
        return None
    return row["value"] if row else None


def _migrate(conn: sqlite3.Connection, from_version: str, to_version: str) -> None:
    migrations = {
        ("1", "2"): _V1_TO_V2_MIGRATION,
        ("2", "3"): _V2_TO_V3_MIGRATION,
    }
    current = from_version
    while current != to_version:
        # step one version at a time
        next_version = str(int(current) + 1)
        key = (current, next_version)
        if key not in migrations:
            raise SchemaError(
                f"no migration path from schema {current} to {next_version}"
            )
        for step in migrations[key]:
            if callable(step):
                step(conn)
            else:
                conn.execute(step)
        current = next_version
        if current == to_version:
            break
        if int(current) > int(to_version):
            raise SchemaError(
                f"no migration path from schema {from_version} to {to_version}"
            )


def init_db(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = get_connection(state_dir)
    # Use explicit transaction boundaries and individual execute() calls.
    # sqlite3.Connection.executescript() issues an implicit COMMIT before it
    # runs, which would break atomicity: schema objects created for an upgrade
    # would survive a later migration failure. Begin an explicit transaction
    # first so that every DDL and DML statement participates in the same
    # transaction and can be rolled back on failure.
    with conn:
        conn.execute("BEGIN")
        # v1 base schema.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                case_number TEXT NOT NULL,
                court TEXT NOT NULL,
                plaintiff TEXT,
                defendant TEXT,
                event_date TEXT,
                event_type TEXT NOT NULL,
                doc_text TEXT,
                imported_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_event_date ON records(event_date);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_event_type ON records(event_type);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS npa_docs (
                doc_id TEXT NOT NULL,
                edition TEXT NOT NULL,
                title TEXT NOT NULL,
                doc_date TEXT,
                body TEXT,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (doc_id, edition)
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_npa_docs_doc_date ON npa_docs(doc_date);"
        )

        # v2 (Phase 3) schema. Created inside the same transaction so that an
        # upgrade failure rolls these objects back along with any data changes.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                case_number TEXT NOT NULL,
                court TEXT NOT NULL,
                plaintiff TEXT,
                defendant TEXT,
                source_name TEXT NOT NULL,
                source_record_id TEXT,
                fetched_at TEXT,
                source_url TEXT,
                imported_at TEXT NOT NULL,
                status TEXT,
                owner TEXT,
                closed_at TEXT,
                instance TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS case_amounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                amount_rub TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                note TEXT,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'planned',
                paid_at TEXT
            );
            """
            )
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS case_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            title TEXT,
            added_at TEXT NOT NULL,
            supersedes INTEGER
        );
        """
        )
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS case_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            author TEXT,
            created_at TEXT NOT NULL,
            supersedes INTEGER REFERENCES case_notes(id)
        );
        """
    )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deadline_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                deadline_key TEXT NOT NULL,
                due_at TEXT,
                basis_hash TEXT,
                state TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT,
                evidence_note TEXT,
                basis TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deadline_states_case ON deadline_states(case_id, deadline_key);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                event_date TEXT,
                event_type TEXT NOT NULL,
                doc_text TEXT,
                source_name TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                fetched_at TEXT,
                source_url TEXT,
                imported_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_case_id ON events(case_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id);"
        )

        # the wave §1: колонки case_amounts должны появиться и на базе, уже
        # заштампованной v3 (миграционный шаг там не повторится). Идемпотентно.
        _ensure_case_amounts_columns(conn)
        # the wave §1+§2: тот же ensure-паттерн для evidence_items (новая таблица)
        # и case_documents.supersedes (новая колонка) — без bump схемы.
        _ensure_evidence_items_table(conn)
        _ensure_case_documents_columns(conn)
        # the wave §1+§2: case_representatives (новая таблица) и cases.instance
        # (новая колонка) — тем же ensure-паттерном, SCHEMA_VERSION = '3'.
        _ensure_case_representatives_table(conn)
        _ensure_cases_columns(conn)

        current = _get_schema_version(conn)
        if current is None:
            # Fresh database: stamp it at the current schema version.
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        elif current == SCHEMA_VERSION:
            pass
        elif current > SCHEMA_VERSION:
            raise SchemaError(
                f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
            )
        else:
            _migrate(conn, current, SCHEMA_VERSION)


def get_connection(state_dir: Path) -> sqlite3.Connection:
    db = _db_path(state_dir)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _doctor_metrics(state_dir: Path) -> Dict[str, Any]:
    """история §5 (S-86): metrics-секция doctor — ленивый импорт и отказ-устойчивость.

    doctor обязан остаться читаемым и на битой базе: падение счётчиков
    превращается в {"error": ...}, а не в исключение из-под doctor.
    """
    try:
        from .metrics import doctor_metrics

        return doctor_metrics(state_dir)
    except Exception as exc:  # sqlite3.Error, OSError, всё то же самое
        return {"error": f"{type(exc).__name__}: {exc}"}


def doctor_check(state_dir: Path) -> Dict[str, Any]:
    from .production_calendar import calendar_check

    calendar = calendar_check()
    report: Dict[str, Any] = {
        "state_dir": str(state_dir),
        "exists": state_dir.exists(),
        "writable": False,
        "db_exists": False,
        "schema_version": None,
        "integrity_ok": False,
        "healthy": False,
        "calendar": calendar,
    }

    try:
        report["writable"] = state_dir.exists() and _is_writable(state_dir)
    except OSError:
        report["writable"] = False

    db = _db_path(state_dir)
    report["db_exists"] = db.exists()

    if not db.exists():
        # A missing DB is not an error for doctor; init will create it.
        report["integrity_ok"] = True
        report["reminders_available"] = 0
        # W6a §4: алерты живут файлами и не зависят от наличия БД — счётчик
        # обязан работать и на этом пути тоже.
        try:
            from .court_alerts import count_pending_alerts
            report["alerts_pending"] = count_pending_alerts(state_dir)
        except Exception:
            report["alerts_pending"] = None
        # история §5 (S-86): metrics-секция аддитивна — пустой state даёт нули,
        # а не отсутствие ключа; пины формата doctor не сдвинуты.
        report["metrics"] = _doctor_metrics(state_dir)
        report["freshness"] = _freshness_report(state_dir)
        report["healthy"] = report["exists"] and report["writable"] and calendar.get("valid", False)
        return report

    try:
        conn = get_connection(state_dir)
        with conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            report["schema_version"] = row["value"] if row else None

            integrity = conn.execute("PRAGMA quick_check").fetchone()
            report["integrity_ok"] = integrity is not None and integrity[0] == "ok"

        # the wave §2: сколько напоминаний реально доступно в типовом окне 7 дней.
        # Только чтение локальной таблицы — шина не трогается.
        try:
            import datetime as _dt
            from .reminders import due_items
            report["reminders_available"] = len(
                due_items(list_all_deadline_states(conn), _dt.date.today(), 7)
            )
        except sqlite3.Error:
            report["reminders_available"] = None

        # W6a §4: локальные алерты case-search, ещё не переданные в шину.
        # Счётчик только читает файлы state-dir/cwd; шина не трогается.
        try:
            from .court_alerts import count_pending_alerts
            report["alerts_pending"] = count_pending_alerts(state_dir)
        except Exception:
            report["alerts_pending"] = None

        # история §5 (S-86): полная metrics-секция (дела по статусам, сроки по
        # state, документы/ревизии, evidence, представители, reminders, last
        # backup из meta) — additive, пины doctor формата не сдвинуты.
        report["metrics"] = _doctor_metrics(state_dir)
    except sqlite3.Error:
        report["integrity_ok"] = False

    report["freshness"] = _freshness_report(state_dir)
    report["healthy"] = (
        report["exists"]
        and report["writable"]
        and report["db_exists"]
        and report["schema_version"] == SCHEMA_VERSION
        and report["integrity_ok"]
        and calendar.get("valid", False)
    )
    return report


def _is_writable(path: Path) -> bool:
    if not path.is_dir():
        return False
    test_file = path / ".write_test"
    try:
        test_file.write_text("1")
        test_file.unlink()
        return True
    except OSError:
        return False


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class SearchError(Exception):
    """Raised when a search cannot be executed safely."""


def _coerce_limit(limit: int) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise SearchError("limit must be an integer")
    if limit <= 0:
        raise SearchError("limit must be positive")
    if limit > MAX_LIMIT:
        raise SearchError(f"limit cannot exceed {MAX_LIMIT}")
    return limit


def _validate_date_bounds(since: Optional[str], until: Optional[str]) -> None:
    since_parsed = None
    until_parsed = None
    for date_value, name in ((since, "since"), (until, "until")):
        if date_value is not None:
            try:
                parsed = datetime.date.fromisoformat(date_value)
            except (TypeError, ValueError) as exc:
                raise SearchError(f"{name} must be YYYY-MM-DD: {date_value}") from exc
            if name == "since":
                since_parsed = parsed
            else:
                until_parsed = parsed
    if since_parsed is not None and until_parsed is not None and since_parsed > until_parsed:
        raise SearchError("since must not be later than until")


def _like_pattern(query: Optional[str]) -> Optional[str]:
    if query is None or query == "":
        return None
    # Escape SQL LIKE wildcards in the user query.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _build_event_match_where(
    like_pattern: Optional[str], since: Optional[str], until: Optional[str]
) -> Tuple[List[str], List[Any]]:
    where_clauses: List[str] = []
    params: List[Any] = []
    if like_pattern is not None:
        where_clauses.append(
            "("
            "e.event_id LIKE ? ESCAPE '\\' OR "
            "e.event_type LIKE ? ESCAPE '\\' OR "
            "e.doc_text LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([like_pattern] * 3)
    if since is not None:
        where_clauses.append("e.event_date >= ?")
        params.append(since)
    if until is not None:
        where_clauses.append("e.event_date <= ?")
        params.append(until)
    return where_clauses, params


def _build_case_match_where(like_pattern: Optional[str]) -> Tuple[List[str], List[Any]]:
    where_clauses: List[str] = []
    params: List[Any] = []
    if like_pattern is not None:
        where_clauses.append(
            "("
            "c.case_id LIKE ? ESCAPE '\\' OR "
            "c.case_number LIKE ? ESCAPE '\\' OR "
            "c.court LIKE ? ESCAPE '\\' OR "
            "c.plaintiff LIKE ? ESCAPE '\\' OR "
            "c.defendant LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([like_pattern] * 5)
    return where_clauses, params


def search_records(
    conn: sqlite3.Connection,
    kind: str,
    query: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if kind != "court":
        raise SearchError(f"unsupported search kind: {kind}")

    limit = _coerce_limit(limit)

    for date_value, name in ((since, "since"), (until, "until")):
        if date_value is not None:
            try:
                datetime.date.fromisoformat(date_value)
            except (TypeError, ValueError) as exc:
                raise SearchError(f"{name} must be YYYY-MM-DD: {date_value}") from exc

    where_clauses: List[str] = []
    params: List[Any] = []

    like_pattern = _like_pattern(query)
    if like_pattern is not None:
        where_clauses.append(
            "("
            "case_number LIKE ? ESCAPE '\\' OR "
            "court LIKE ? ESCAPE '\\' OR "
            "event_type LIKE ? ESCAPE '\\' OR "
            "doc_text LIKE ? ESCAPE '\\' OR "
            "plaintiff LIKE ? ESCAPE '\\' OR "
            "defendant LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([like_pattern] * 6)

    if since is not None:
        where_clauses.append("event_date >= ?")
        params.append(since)
    if until is not None:
        where_clauses.append("event_date <= ?")
        params.append(until)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    count_sql = f"SELECT COUNT(*) FROM records {where_sql}"
    total = conn.execute(count_sql, params).fetchone()[0]

    select_sql = (
        f"SELECT * FROM records {where_sql} "
        f"ORDER BY event_date ASC, record_id ASC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(select_sql, params).fetchall()
    results = [row_to_dict(r) for r in rows]

    meta = {
        "limit": limit,
        "total": total,
        "truncated": total > limit,
    }
    return results, meta


def search_npa(
    conn: sqlite3.Connection, query: Optional[str] = None, limit: int = DEFAULT_LIMIT
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    limit = _coerce_limit(limit)

    where_clauses: List[str] = []
    params: List[Any] = []

    like_pattern = _like_pattern(query)
    if like_pattern is not None:
        where_clauses.append(
            "(doc_id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
        )
        params.extend([like_pattern] * 3)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM npa_docs {where_sql}", params
    ).fetchone()[0]

    select_sql = (
        f"SELECT * FROM npa_docs {where_sql} "
        f"ORDER BY doc_date ASC, doc_id ASC, edition ASC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(select_sql, params).fetchall()
    results = [row_to_dict(r) for r in rows]

    meta = {
        "limit": limit,
        "total": total,
        "truncated": total > limit,
    }
    return results, meta


def compare_npa_editions(
    conn: sqlite3.Connection, doc_id: str, edition1: str, edition2: str
) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM npa_docs WHERE doc_id = ? AND edition IN (?, ?)",
        (doc_id, edition1, edition2),
    ).fetchall()
    by_edition = {row["edition"]: row_to_dict(row) for row in rows}

    if edition1 not in by_edition or edition2 not in by_edition:
        missing = [e for e in (edition1, edition2) if e not in by_edition]
        raise SearchError(f"editions not found for doc_id={doc_id}: {missing}")

    doc1 = by_edition[edition1]
    doc2 = by_edition[edition2]

    return {
        "doc_id": doc_id,
        "edition1": edition1,
        "edition2": edition2,
        "title1": doc1["title"],
        "title2": doc2["title"],
        "date1": doc1["doc_date"],
        "date2": doc2["doc_date"],
        "body1_length": len(doc1.get("body") or ""),
        "body2_length": len(doc2.get("body") or ""),
        "same_body": doc1.get("body") == doc2.get("body"),
    }


# ---------------------------------------------------------------------------
# Native court case/event timeline (Phase 3)
# ---------------------------------------------------------------------------


def search_court_cases(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Search native court cases and events locally.

    Returns one row per matching case with a bounded list of matching event
    metadata (event_id, event_type, event_date, snippet). The full event
    document text is never returned.
    """
    limit = _coerce_limit(limit)
    _validate_date_bounds(since, until)

    like_pattern = _like_pattern(query)
    has_query = like_pattern is not None
    has_date_filter = since is not None or until is not None

    if not has_query and not has_date_filter:
        raise SearchError("query is required unless a date bound is provided")

    event_where, event_params = _build_event_match_where(like_pattern, since, until)
    case_where, case_params = _build_case_match_where(like_pattern)

    event_where_sql = " AND ".join(event_where) if event_where else "1=1"
    case_where_sql = " AND ".join(case_where) if case_where else "0=1"

    # DISTINCT matching case IDs with aggregate info. Cases that match only by
    # metadata appear via LEFT JOIN with matching_event_count = 0.
    select_sql = f"""
    WITH
    matching_events AS (
        SELECT
            e.event_id,
            e.case_id,
            e.event_date,
            e.event_type,
            substr(coalesce(e.doc_text, ''), 1, 200) AS snippet
        FROM events e
        WHERE {event_where_sql}
    ),
    matching_cases AS (
        SELECT c.case_id
        FROM cases c
        WHERE {case_where_sql}
    ),
    all_matches AS (
        SELECT case_id FROM matching_cases
        UNION
        SELECT case_id FROM matching_events
    ),
    case_summary AS (
        SELECT
            m.case_id,
            COUNT(me.event_id) AS matching_event_count,
            MAX(me.event_date) AS most_recent_event_date
        FROM all_matches m
        LEFT JOIN matching_events me ON me.case_id = m.case_id
        GROUP BY m.case_id
    )
    SELECT
        c.case_id, c.case_number, c.court, c.plaintiff, c.defendant,
        c.source_name, c.source_record_id, c.fetched_at, c.source_url, c.imported_at,
        s.matching_event_count, s.most_recent_event_date
    FROM case_summary s
    JOIN cases c ON c.case_id = s.case_id
    ORDER BY
        CASE WHEN s.most_recent_event_date IS NULL THEN 1 ELSE 0 END,
        s.most_recent_event_date DESC,
        c.case_id ASC
    LIMIT ?
    """
    params = event_params + case_params + [limit]
    rows = conn.execute(select_sql, params).fetchall()

    results: List[Dict[str, Any]] = []
    for row in rows:
        case_dict: Dict[str, Any] = {
            "case_id": row["case_id"],
            "case_number": row["case_number"],
            "court": row["court"],
            "plaintiff": row["plaintiff"],
            "defendant": row["defendant"],
            "source_name": row["source_name"],
            "source_record_id": row["source_record_id"],
            "fetched_at": row["fetched_at"],
            "source_url": row["source_url"],
            "imported_at": row["imported_at"],
            "matching_event_count": row["matching_event_count"],
        }
        # Fetch a short list of matching events for this case. This repeats the
        # event match predicate so the returned list is bounded and consistent.
        event_sql = f"""
        SELECT
            e.event_id,
            e.event_date,
            e.event_type,
            substr(coalesce(e.doc_text, ''), 1, 200) AS snippet
        FROM events e
        WHERE e.case_id = ? AND ({event_where_sql})
        ORDER BY
            CASE WHEN e.event_date IS NULL THEN 1 ELSE 0 END,
            e.event_date DESC,
            e.event_id ASC
        LIMIT 5
        """
        event_rows = conn.execute(event_sql, [row["case_id"]] + event_params).fetchall()
        case_dict["matching_events"] = [
            {
                "event_id": er["event_id"],
                "event_type": er["event_type"],
                "event_date": er["event_date"],
                "snippet": er["snippet"],
            }
            for er in event_rows
        ]
        results.append(case_dict)

    # Total distinct matching cases for the truncation flag.
    total_sql = f"""
    WITH
    matching_events AS (
        SELECT e.case_id
        FROM events e
        WHERE {event_where_sql}
    ),
    matching_cases AS (
        SELECT c.case_id
        FROM cases c
        WHERE {case_where_sql}
    ),
    all_matches AS (
        SELECT case_id FROM matching_cases
        UNION
        SELECT case_id FROM matching_events
    )
    SELECT COUNT(*) FROM all_matches
    """
    total = conn.execute(total_sql, event_params + case_params).fetchone()[0]

    meta = {
        "limit": limit,
        "total": total,
        "truncated": total > limit,
    }
    return results, meta


def get_case(conn: sqlite3.Connection, case_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    return row_to_dict(row)


def get_case_summary(conn: sqlite3.Connection, case_id: str) -> Optional[Dict[str, Any]]:
    return get_case(conn, case_id)


def get_case_events(conn: sqlite3.Connection, case_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT event_id, case_id, event_date, event_type, doc_text,
               source_name, source_record_id, fetched_at, source_url, imported_at
        FROM events WHERE case_id = ?
        ORDER BY event_date ASC, event_id ASC
        """,
        (case_id,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]



_UNSET = object()

LEGAL_CASE_STATUSES = ["new", "review", "filed", "pending", "decided", "appealed", "closed", "unknown"]


class CaseError(Exception):
    pass


class InvalidStatusError(CaseError, ValueError):
    pass


class InvalidAmountError(CaseError, ValueError):
    pass


class InvalidInstanceError(CaseError, ValueError):
    """the wave §1: instance дела вне {apk, gpk, kas, unknown} — отказ на записи.

    Матрица (the wave) уже отказывает при чтении с тем же набором значений;
    отказ на записи не даёт завести дело, которое потом ничем не открыть.
    """


class DocNotFoundError(CaseError, ValueError):
    pass


class InvalidRepresentativeSideError(CaseError, ValueError):
    """the wave §2: side вне {plaintiff, defendant} — отказ, а не догадка."""


class PurgeError(CaseError, ValueError):
    """история §5 (S-07): purge без явного подтверждения."""


#: Поле дела «инстанция» (the wave читает его же): значения — из INSTANCE_TO_JURISDICTION.
CASE_INSTANCES = ("apk", "gpk", "kas", "unknown")

#: the wave §2 (S-15): стороны, для которых заводят представителя. Проверка
#: полномочий НЕ автоматическая — этот набор только про то, к какой стороне
#: строка относится.
CASE_SIDES = ("plaintiff", "defendant")

REPRESENTATIVE_HINT = (
    "Полномочия проверяются вручную: sud-cli хранит описание доверенности как "
    "текст и не сверяет её ни с каким реестром — ни автоматической проверки "
    "полномочий, ни вывода о допустимости представителя здесь нет"
)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _new_case_id() -> str:
    import uuid
    return "case-" + uuid.uuid4().hex[:12]


def create_case(conn, case_number, court, plaintiff=None, defendant=None, owner=None, status=None, closed_at=None, instance=None, **extra):
    if status is not None and status not in LEGAL_CASE_STATUSES:
        raise InvalidStatusError("invalid_status")
    for k in extra:
        if k not in ("status", "owner", "closed_at", "instance"):
            raise ValueError(f"unknown field: {k}")
    if "status" in extra:
        status = extra["status"]
        if status is not None and status not in LEGAL_CASE_STATUSES:
            raise InvalidStatusError("invalid_status")
    if "owner" in extra:
        owner = extra["owner"]
    if "closed_at" in extra:
        closed_at = extra["closed_at"]
    if "instance" in extra:
        instance = extra["instance"]
    # the wave §1: instance валидируется на записи тем же набором, что и матрица
    # при чтении (apk|gpk|kas|unknown) — NULL = «не указана».
    if instance is not None and str(instance).strip().lower() not in CASE_INSTANCES:
        raise InvalidInstanceError(f"invalid_instance: {instance}")
    row = conn.execute(
        "SELECT case_id FROM cases WHERE case_number = ? AND court = ?",
        (case_number, court),
    ).fetchone()
    if row is not None:
        return {
            "case_id": row["case_id"],
            "case_number": case_number,
            "court": court,
            "created": False,
            "duplicate": True,
            "status": "duplicate_window",
        }
    case_id = _new_case_id()
    conn.execute(
        """
        INSERT INTO cases
            (case_id, case_number, court, plaintiff, defendant, source_name,
             source_record_id, fetched_at, source_url, imported_at, status, owner,
             closed_at, instance)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, case_number, court, plaintiff, defendant, "local-registry",
         None, None, None, _now_iso(), status, owner, closed_at, instance),
    )
    conn.commit()
    return {
        "case_id": case_id,
        "case_number": case_number,
        "court": court,
        "plaintiff": plaintiff,
        "defendant": defendant,
        "status": status,
        "owner": owner,
        "closed_at": closed_at,
        "created": True,
        "duplicate": False,
    }


def set_case(conn, case_id, status=_UNSET, owner=_UNSET, closed_at=_UNSET,
             instance=_UNSET, **extra):
    allowed = {"status", "owner", "closed_at", "instance"}
    fields = {}
    if status is not _UNSET:
        fields["status"] = status
    if owner is not _UNSET:
        fields["owner"] = owner
    if closed_at is not _UNSET:
        fields["closed_at"] = closed_at
    if instance is not _UNSET:
        fields["instance"] = instance
    for k, v in extra.items():
        if k not in allowed:
            raise ValueError(f"unknown field: {k}")
        fields[k] = v
    if "status" in fields and fields["status"] is not None and fields["status"] not in LEGAL_CASE_STATUSES:
        raise InvalidStatusError("invalid_status")
    if "instance" in fields and fields["instance"] is not None \
            and str(fields["instance"]).strip().lower() not in CASE_INSTANCES:
        raise InvalidInstanceError(f"invalid_instance: {fields['instance']}")
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE cases SET {sets} WHERE case_id = ?",
            list(fields.values()) + [case_id],
        )
        conn.commit()
    return get_case(conn, case_id)


def list_cases(conn, status=None, owner=None, court=None, party=None):
    where = []
    params = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if owner is not None:
        where.append("owner = ?")
        params.append(owner)
    if court is not None:
        where.append("court LIKE ?")
        params.append(f"%{court}%")
    if party is not None:
        where.append("(plaintiff LIKE ? OR defendant LIKE ?)")
        params.extend([f"%{party}%", f"%{party}%"])
    sql = "SELECT * FROM cases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY case_id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]


def add_case_amount(conn, case_id, kind, amount, note=None, currency="RUB"):
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    from decimal import Decimal, InvalidOperation
    text = amount.strip() if isinstance(amount, str) else str(amount)
    text = text.replace(",", ".")
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAmountError("invalid_amount") from exc
    if not dec.is_finite():
        raise InvalidAmountError("invalid_amount")
    conn.execute(
        """
        INSERT INTO case_amounts (case_id, kind, amount_rub, currency, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, kind, str(dec), currency, note, _now_iso()),
    )
    conn.commit()
    r = conn.execute(
        "SELECT * FROM case_amounts WHERE id = last_insert_rowid()"
    ).fetchone()
    return row_to_dict(r)


def list_case_amounts(conn, case_id):
    rows = conn.execute(
        "SELECT * FROM case_amounts WHERE case_id = ? ORDER BY id ASC", (case_id,)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


# the wave §1: статусы платежей. Проверка значения на входе — это валидация
# аргумента CLI, а NOT enum в схеме: kind и status остаются свободным TEXT.
AMOUNT_STATUSES = ("planned", "paid", "refunded")


def set_case_amount(conn, case_id, amount_id, status=None, paid_at=None):
    row = conn.execute(
        "SELECT * FROM case_amounts WHERE id = ? AND case_id = ?",
        (amount_id, case_id),
    ).fetchone()
    if row is None:
        return None
    if status is not None and status not in AMOUNT_STATUSES:
        raise InvalidAmountError("invalid_amount_status")
    sets, params = [], []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if paid_at is not None:
        sets.append("paid_at = ?")
        params.append(paid_at)
    if sets:
        params.append(amount_id)
        conn.execute(
            f"UPDATE case_amounts SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    r = conn.execute("SELECT * FROM case_amounts WHERE id = ?", (amount_id,)).fetchone()
    return row_to_dict(r)


def summarize_case_amounts(conn, case_id):
    """Decimal-точные суммы по kind x status (the wave §1).

    amount_rub хранится строкой: переводим ровно в Decimal, float не
    участвует нигде — иначе 0.1+0.2 даст хвост двоичной дроби.
    """
    from decimal import Decimal
    rows = list_case_amounts(conn, case_id)
    groups = {}
    totals_by_status = {}
    for r in rows:
        status = r.get("status") or "planned"
        key = (r["kind"], status)
        dec = Decimal(str(r["amount_rub"]))
        cur, cnt = groups.get(key, (Decimal("0"), 0))
        groups[key] = (cur + dec, cnt + 1)
        cur, cnt = totals_by_status.get(status, (Decimal("0"), 0))
        totals_by_status[status] = (cur + dec, cnt + 1)
    out_rows = [
        {"kind": kind, "status": status, "total": str(total), "count": cnt}
        for (kind, status), (total, cnt) in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]
    return {
        "case_id": case_id,
        "rows": out_rows,
        "totals_by_status": {
            status: str(total)
            for status, (total, _cnt) in sorted(totals_by_status.items())
        },
    }


def _file_sha256(path) -> str:
    """Chunked sha256 файла — один источник хэша для docs и evidence (the wave)."""
    import hashlib
    from pathlib import Path as _Path
    h = hashlib.sha256()
    with _Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _superseded_ids(conn, case_id=None):
    """Множество id, на которые ссылается хотя бы одна supersedes-строка."""
    sql = "SELECT DISTINCT supersedes FROM case_documents WHERE supersedes IS NOT NULL"
    params: list = []
    if case_id is not None:
        sql += " AND case_id = ?"
        params.append(case_id)
    return {r["supersedes"] for r in conn.execute(sql, params).fetchall()}


def current_case_documents(conn, case_id):
    """the wave §2: «текущий» документ = последняя не-superseded запись (added_at,id)."""
    rows = list_case_documents(conn, case_id)
    gone = _superseded_ids(conn)
    live = [r for r in rows if r["id"] not in gone]
    live.sort(key=lambda r: (str(r.get("added_at") or ""), int(r["id"])), reverse=True)
    return live


def case_document_history(conn, case_id, path=None):
    """the wave §2: цепочка ревизий newest-first с хэшами; опц. фильтр по path.

    Цепочка линейная: новая ревизия ссылается supersedes ровно на одну
    предыдущую, старые строки никогда не мутируются.
    """
    rows = list_case_documents(conn, case_id)
    if path is not None:
        rows = [r for r in rows if r["path"] == str(path)]
    rows.sort(key=lambda r: (str(r.get("added_at") or ""), int(r["id"])), reverse=True)
    gone = _superseded_ids(conn)
    out = []
    for i, r in enumerate(rows):
        r = dict(r)
        r["revision"] = len(rows) - i  # 1 = первая ревизия
        r["current"] = r["id"] not in gone
        out.append(r)
    return out


def add_case_document(conn, case_id, path, title=None, supersedes=None):
    """Добавить документ; the wave §2: повтор того же path = новая ревизия.

    supersedes=None и файл уже добавлен этому делу -> строка-ревизия
    автоматически ссылается supersedes на текущую (head) запись того же path.
    Явный supersedes=<id> проверяется на существование и принадлежность делу.
    Существующие строки не меняются никогда.
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    from pathlib import Path as _Path
    p = _Path(path)
    if not p.is_file():
        raise DocNotFoundError("doc_not_found")
    if supersedes is not None:
        prev = conn.execute(
            "SELECT * FROM case_documents WHERE id = ?", (int(supersedes),)
        ).fetchone()
        if prev is None:
            raise DocNotFoundError("supersedes_id_not_found")
        if prev["case_id"] != case_id:
            raise DocNotFoundError("supersedes_other_case")
    else:
        head = [r for r in current_case_documents(conn, case_id)
                if r["path"] == str(path)]
        supersedes = head[0]["id"] if head else None
    conn.execute(
        """
        INSERT INTO case_documents (case_id, path, sha256, title, added_at, supersedes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, str(path), _file_sha256(path), title, _now_iso(), supersedes),
    )
    conn.commit()
    r = conn.execute(
        "SELECT * FROM case_documents WHERE id = last_insert_rowid()"
    ).fetchone()
    return row_to_dict(r)


def list_case_documents(conn, case_id):
    rows = conn.execute(
        "SELECT * FROM case_documents WHERE case_id = ? ORDER BY id ASC", (case_id,)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_case_document(conn, doc_id, case_id=None):
    """the wave §3: строка case_documents по id (опц. проверка принадлежности делу)."""
    row = conn.execute("SELECT * FROM case_documents WHERE id = ?", (int(doc_id),)).fetchone()
    if row is None:
        return None
    d = row_to_dict(row)
    if case_id is not None and d["case_id"] != case_id:
        return None
    return d


# ---------------------------------------------------------------------------
# the wave §1 (S-73): реестр доказательств — том/лист, sha256-целостность
# ---------------------------------------------------------------------------

EVIDENCE_MEDIA = ("paper", "electronic")


class EvidenceError(CaseError, ValueError):
    pass


def _numeric_aware_key(value):
    """Ключ natural-sort: выделенные цифры — int, остальное — регистронезависимая строка.

    '2' < '10' по числу, а не по лексикографии; None -> '' (первым). Смешанные
    chunk'и сравниваются флагом (0=число, 1=текст), чтобы не ронять py3-сравнение int/str.
    """
    import re
    text = "" if value is None else str(value)
    parts = re.split(r"(\d+)", text)
    key = []
    for part in parts:
        if part == "":
            continue
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part.lower()))
    return tuple(key)


def add_case_evidence(conn, case_id, title, path=None, volume=None, sheet=None,
                      media=None, note=None):
    """Добавить доказательство. --path => sha256 файла + doc_ref.

    doc_ref — «если ещё не добавлен»: если текущая не-superseded строка
    case_documents с тем же path для этого дела уже есть, ссылка переиспользуется;
    иначе файл добавляется как документ (обычным add_case_document, с ревизиями).
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    if not title or not str(title).strip():
        raise EvidenceError("title is required")
    if media is not None and media not in EVIDENCE_MEDIA:
        raise EvidenceError(f"invalid_media: {media}")
    sha256 = None
    doc_ref = None
    if path is not None:
        from pathlib import Path as _Path
        p = _Path(path)
        if not p.is_file():
            raise DocNotFoundError("doc_not_found")
        sha256 = _file_sha256(p)
        existing = [r for r in current_case_documents(conn, case_id)
                    if r["path"] == str(path)]
        if existing:
            doc_ref = existing[0]["id"]
        else:
            doc = add_case_document(conn, case_id, path)
            doc_ref = doc["id"]
    conn.execute(
        """
        INSERT INTO evidence_items
            (case_id, title, volume, sheet, doc_ref, sha256, media, added_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, title, volume, sheet, doc_ref, sha256, media, _now_iso(), note),
    )
    conn.commit()
    r = conn.execute(
        "SELECT * FROM evidence_items WHERE id = last_insert_rowid()"
    ).fetchone()
    return row_to_dict(r)


def list_case_evidence(conn, case_id):
    """Список доказательств: сортировка том -> лист, numerical-aware, затем id."""
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM evidence_items WHERE case_id = ?", (case_id,)
    ).fetchall()]
    rows.sort(key=lambda r: (_numeric_aware_key(r.get("volume")),
                             _numeric_aware_key(r.get("sheet")),
                             int(r["id"])))
    return rows


def verify_case_evidence(conn, case_id):
    """Пересчёт sha256 для items с path; verdict per item.

    intact / CHANGED (хэш не совпал) / MISSING_FILE (путь пропал) /
    unchecked (item без path — бумажное: не провал, а «не проверено»).
    """
    from pathlib import Path as _Path
    items = []
    for r in list_case_evidence(conn, case_id):
        item = dict(r)
        path = None
        if r["doc_ref"] is not None:
            drow = conn.execute(
                "SELECT path FROM case_documents WHERE id = ?", (r["doc_ref"],)
            ).fetchone()
            path = drow["path"] if drow else None
        item["path"] = path
        if r["sha256"] is None or path is None:
            item["verdict"] = "unchecked"
        elif not _Path(path).is_file():
            item["verdict"] = "MISSING_FILE"
        else:
            try:
                actual = _file_sha256(path)
            except OSError:
                item["verdict"] = "MISSING_FILE"
            else:
                item["verdict"] = "intact" if actual == r["sha256"] else "CHANGED"
        items.append(item)
    return items


def evidence_summary(items):
    counts = {"intact": 0, "CHANGED": 0, "MISSING_FILE": 0, "unchecked": 0}
    for i in items:
        counts[i["verdict"]] += 1
    problems = [i for i in items if i["verdict"] in ("CHANGED", "MISSING_FILE")]
    return {
        "total": len(items),
        "intact": counts["intact"],
        "changed": counts["CHANGED"],
        "missing_file": counts["MISSING_FILE"],
        "unchecked": counts["unchecked"],
        "problems": len(problems),
        "problem_items": problems,
    }


# ---------------------------------------------------------------------------
# the wave §2 (S-15): представители по делу
# ---------------------------------------------------------------------------


def add_case_representative(conn, case_id, side, name, authority_note=None,
                            power_docs=None):
    """Завести представителя. Полномочия НЕ проверяются — это хранилище текста.

    side валидируется на входе ({plaintiff, defendant}, регистр не важен):
    строка «side=третейский» потом читалась бы как участие в деле, которого
    никто не заводил. name обязателен — представитель без имени в карточке
    выглядит как подтверждённое лицо.
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    normalized = ("" if side is None else str(side).strip().lower())
    if normalized not in CASE_SIDES:
        raise InvalidRepresentativeSideError(
            f"invalid_side: {side!r} (допустимо: {', '.join(CASE_SIDES)})"
        )
    if not name or not str(name).strip():
        raise CaseError("name is required")
    conn.execute(
        """
        INSERT INTO case_representatives
            (case_id, side, name, authority_note, power_docs, added_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, normalized, str(name).strip(), authority_note, power_docs,
         _now_iso()),
    )
    conn.commit()
    r = conn.execute(
        "SELECT * FROM case_representatives WHERE id = last_insert_rowid()"
    ).fetchone()
    return row_to_dict(r)


def list_case_representatives(conn, case_id):
    """Представители дела: side -> id (детерминированный порядок, без магии)."""
    rows = conn.execute(
        "SELECT * FROM case_representatives WHERE case_id = ? ORDER BY side ASC, id ASC",
        (case_id,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def representatives_card_section(conn, case_id):
    """Секция card: список + явный hint, что проверка полномочий ручная."""
    items = list_case_representatives(conn, case_id)
    by_side = {side: 0 for side in CASE_SIDES}
    for item in items:
        by_side[item["side"]] = by_side.get(item["side"], 0) + 1
    return {
        "count": len(items),
        "by_side": by_side,
        "items": items,
        "hint": REPRESENTATIVE_HINT,
    }


def add_case_note(conn, case_id, text, author=None, supersedes=None):
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    conn.execute(
        """
        INSERT INTO case_notes (case_id, text, author, created_at, supersedes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, text, author, _now_iso(), supersedes),
    )
    conn.commit()
    r = conn.execute(
        "SELECT * FROM case_notes WHERE id = last_insert_rowid()"
    ).fetchone()
    return row_to_dict(r)


def list_case_notes(conn, case_id, limit=None):
    sql = "SELECT * FROM case_notes WHERE case_id = ? ORDER BY id ASC"
    params = [case_id]
    if limit is not None:
        sql = "SELECT * FROM case_notes WHERE case_id = ? ORDER BY id DESC LIMIT ?"
        params = [case_id, int(limit)]
        rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(r) for r in reversed(rows)]
    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]


def supersede_case_note(conn, note_id, text, author=None):
    row = conn.execute("SELECT * FROM case_notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return None
    case_id = row["case_id"]
    orig_author = row["author"]
    new_author = author if author is not None else orig_author
    return add_case_note(conn, case_id, text, author=new_author, supersedes=note_id)


def get_case_card(conn, case_id, note_limit=5):
    case = get_case(conn, case_id)
    if case is None:
        return None
    amounts = list_case_amounts(conn, case_id)
    documents = list_case_documents(conn, case_id)
    notes = list_case_notes(conn, case_id, limit=note_limit)
    events = get_case_events(conn, case_id)
    source = {
        "source_name": case.get("source_name"),
        "source_record_id": case.get("source_record_id"),
        "fetched_at": case.get("fetched_at"),
        "source_url": case.get("source_url"),
        "imported_at": case.get("imported_at"),
    }
    return {
        "case_id": case.get("case_id"),
        "case_number": case.get("case_number"),
        "court": case.get("court"),
        "plaintiff": case.get("plaintiff"),
        "defendant": case.get("defendant"),
        "status": case.get("status"),
        "owner": case.get("owner"),
        "closed_at": case.get("closed_at"),
        "case": case,
        "parties": {"plaintiff": case.get("plaintiff"), "defendant": case.get("defendant")},
        "amounts": amounts,
        "documents": documents,
        # the wave §2: текущие (не-superseded) ревизии — карточка не доминируется
        # историей: полный список remains в documents, subset — здесь.
        "documents_current": current_case_documents(conn, case_id),
        "notes": notes,
        "events": events,
        "source": source,
        "provenance": source,
        "next_deadline": None,
        "deadlines": list_deadline_states(conn, case_id),
        # the wave §1: секция evidence — счётчик + проблемные items (пересчёт
        # integrity только для items с path; бумажные — unchecked).
        "evidence": _evidence_card_section(conn, case_id),
        # the wave §2 (S-15): секция представителей — список + счётчик по сторонам
        # + hint, что полномочия проверяются вручную (автоматической проверки
        # sud не делает и делать не будет).
        "representatives": representatives_card_section(conn, case_id),
    }


def _evidence_card_section(conn, case_id):
    items = verify_case_evidence(conn, case_id)
    summary = evidence_summary(items)
    return {
        "count": summary["total"],
        "problems": summary["problem_items"],
        "summary": {k: v for k, v in summary.items() if k != "problem_items"},
    }


# ---------------------------------------------------------------------------
# Deadline confirmation state (v2.5.0 anti-laches)
# ---------------------------------------------------------------------------

import json as _json


def _deadline_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _basis_hash(basis) -> str:
    import hashlib
    raw = _json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_deadline_state(row) -> dict:
    if row is None:
        return None
    d = row_to_dict(row)
    if d.get("basis"):
        try:
            d["basis"] = _json.loads(d["basis"])
        except (TypeError, ValueError):
            pass
    return d


def list_deadline_states(conn, case_id, deadline_key=None) -> list:
    sql = "SELECT * FROM deadline_states WHERE case_id = ?"
    params = [case_id]
    if deadline_key is not None:
        sql += " AND deadline_key = ?"
        params.append(deadline_key)
    sql += " ORDER BY id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_deadline_state(r) for r in rows]


def list_all_deadline_states(conn) -> list:
    rows = conn.execute("SELECT * FROM deadline_states ORDER BY id ASC").fetchall()
    return [_row_to_deadline_state(r) for r in rows]


def get_latest_deadline_state(conn, case_id, deadline_key):
    row = conn.execute(
        "SELECT * FROM deadline_states WHERE case_id = ? AND deadline_key = ? ORDER BY id DESC LIMIT 1",
        (case_id, deadline_key),
    ).fetchone()
    return _row_to_deadline_state(row)


def get_latest_anchored_state(conn, case_id, deadline_key):
    rows = conn.execute(
        "SELECT * FROM deadline_states WHERE case_id = ? AND deadline_key = ? ORDER BY id DESC",
        (case_id, deadline_key),
    ).fetchall()
    for r in rows:
        d = _row_to_deadline_state(r)
        basis = d.get("basis") or {}
        if isinstance(basis, dict) and "method" in basis:
            return d
    return None


def append_deadline_audit(state_dir, entry) -> None:
    from pathlib import Path
    p = Path(state_dir) / "deadline_audit.jsonl"
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def read_deadline_audit(state_dir, case_id=None) -> list:
    from pathlib import Path
    p = Path(state_dir) / "deadline_audit.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        if case_id is not None and obj.get("case") != case_id:
            continue
        out.append(obj)
    return out


def insert_deadline_state(conn, case_id, deadline_key, due_at=None, basis=None,
                          state="computed", confirmed_by=None, confirmed_at=None,
                          evidence_note=None, state_dir=None, actor=None,
                          action=None, before=None) -> dict:
    basis_json = _json.dumps(basis, ensure_ascii=False, default=str) if basis is not None else None
    bh = _basis_hash(basis) if basis is not None else None
    conn.execute(
        """
        INSERT INTO deadline_states
            (case_id, deadline_key, due_at, basis_hash, state, confirmed_by,
             confirmed_at, evidence_note, basis, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, deadline_key, due_at, bh, state, confirmed_by, confirmed_at,
         evidence_note, basis_json, _deadline_now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM deadline_states WHERE id = last_insert_rowid()"
    ).fetchone()
    d = _row_to_deadline_state(row)
    if state_dir is not None and action is not None:
        append_deadline_audit(state_dir, {
            "ts": _deadline_now_iso(),
            "actor": actor if actor is not None else (confirmed_by if confirmed_by is not None else "system"),
            "case": case_id,
            "key": deadline_key,
            "action": action,
            "before": before,
            "after": d,
        })
    return d


def anchor_deadline_state(conn, state_dir, case_id, deadline_key, event_date,
                          method, evidence, actor=None) -> dict:
    if actor is None:
        actor = "cli"
    # правило «тексты только из реестра»: неизвестный ключ матрицы = отказ, а не молчаливый due_at=None
    from datetime import date as _date
    from .legal_deadlines import calculate_deadline
    from .legal_engine import load_rules
    rules = load_rules()
    matrix = getattr(rules, "deadline_matrix", None) or (rules.get("deadline_matrix") if isinstance(rules, dict) else None)
    if matrix is not None and deadline_key not in matrix:
        raise ValueError(f"unknown_deadline_key: {deadline_key}")
    due_at = None
    try:
        res = calculate_deadline(deadline_key=deadline_key, event_date=_date.fromisoformat(event_date), rules=rules)
        if getattr(res, "status", None) == "ok" and getattr(res, "deadline", None) is not None:
            due_at = res.deadline.isoformat()
    except Exception:
        due_at = None
    before_latest = get_latest_deadline_state(conn, case_id, deadline_key)
    basis = {
        "event_date": event_date,
        "method": method,
        "evidence": evidence,
        "actor": actor,
        "ts": _deadline_now_iso(),
    }
    return insert_deadline_state(
        conn, case_id, deadline_key, due_at=due_at, basis=basis, state="computed",
        evidence_note=evidence,
        state_dir=state_dir, actor=actor, action="anchor", before=before_latest,
    )


def confirm_deadline_state(conn, state_dir, case_id, deadline_key, actor,
                           evidence=None, waive=False, norm=None) -> dict:
    if actor is None:
        actor = "cli"
    state = "waived" if waive else "confirmed"
    note = norm if norm is not None else evidence
    if norm is not None and evidence is not None:
        note = f"{norm}; {evidence}"
    latest = get_latest_deadline_state(conn, case_id, deadline_key)
    before = latest
    now = _deadline_now_iso()
    if latest is not None:
        conn.execute(
            """
            UPDATE deadline_states
               SET state = ?, confirmed_by = ?, confirmed_at = ?, evidence_note = ?
             WHERE id = ?
            """,
            (state, actor, now, note, latest["id"]),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM deadline_states WHERE id = ?", (latest["id"],)
        ).fetchone()
        after = _row_to_deadline_state(row)
    else:
        basis = None
        due_at = None
        after = insert_deadline_state(
            conn, case_id, deadline_key, due_at=due_at, basis=basis, state=state,
            confirmed_by=actor, confirmed_at=now, evidence_note=note,
            state_dir=None, actor=actor, action=None,
        )
    append_deadline_audit(state_dir, {
        "ts": now,
        "actor": actor,
        "case": case_id,
        "key": deadline_key,
        "action": "waive" if waive else "confirm",
        "before": before,
        "after": after,
    })
    return after


# aliases
def waive_deadline_state(conn, state_dir, case_id, deadline_key, actor, norm, evidence=None):
    return confirm_deadline_state(conn, state_dir, case_id, deadline_key, actor,
                                 evidence=evidence, waive=True, norm=norm)


def deadline_states_for_case(conn, case_id, deadline_key=None):
    return list_deadline_states(conn, case_id, deadline_key=deadline_key)


def _freshness_report(state_dir) -> dict:
    import json as _j
    import time as _t
    from datetime import date as _date, datetime as _datetime
    from pathlib import Path

    state_dir = Path(state_dir)
    out = {"warnings": []}
    warnings = []

    # key_rates.json age
    kr_path = Path(__file__).resolve().parent / "data" / "key_rates.json"
    kr = {}
    try:
        kr_text = kr_path.read_text(encoding="utf-8")
        kr = _j.loads(kr_text)
        mtime = kr_path.stat().st_mtime
        age_days = (_t.time() - mtime) / 86400.0
        data_age = None
        for field in ("as_of", "coverage_latest_change", "generated_at", "verified_at"):
            if field in kr:
                try:
                    data_age = (_date.today() - _date.fromisoformat(str(kr[field])[:10])).days
                except ValueError:
                    pass
                if data_age is not None:
                    break
        stale = bool(age_days > 180 or (data_age is not None and data_age > 180))
        if stale:
            warnings.append("key_rates.json older than 180 days")
        kr_info = {
            "age_days": round(age_days, 1),
            "data_age_days": data_age,
            "stale": stale,
        }
        if "provenance" in kr:
            kr_info["provenance"] = kr["provenance"]
    except Exception as exc:
        kr_info = {"error": str(exc), "stale": True}
        warnings.append("key_rates.json unavailable")
    out["key_rates"] = kr_info

    # holidays transferred verified_through
    hol_path = Path(__file__).resolve().parent / "holidays_rf.json"
    try:
        hol = _j.loads(hol_path.read_text(encoding="utf-8"))
        vt = hol.get("verified_through")
        years = hol.get("years_with_verified_transfers")
        current_year = _date.today().year
        stale_h = vt is None or (isinstance(vt, int) and vt < current_year)
        if stale_h:
            warnings.append("holidays transferred verified_through older than current year")
        out["holidays"] = {
            "verified_through": vt,
            "transferred_verified_through": vt,
            "years_with_verified_transfers": years,
            "stale": bool(stale_h),
        }
    except Exception as exc:
        out["holidays"] = {"error": str(exc), "stale": True}
        warnings.append("holidays file unavailable")

    # rules version stamps
    rules_path = Path(__file__).resolve().parent / "legal_rules.json"
    try:
        rules = _j.loads(rules_path.read_text(encoding="utf-8"))
        out["rules"] = {
            "version": rules.get("version"),
            "spec_version": rules.get("spec_version"),
            "tariff_version": rules.get("tariff_version"),
            "legal_status": rules.get("legal_status"),
        }
    except Exception as exc:
        out["rules"] = {"error": str(exc)}

    # deadline audit age
    audit_path = state_dir / "deadline_audit.jsonl"
    if audit_path.exists():
        try:
            age = (_t.time() - audit_path.stat().st_mtime) / 86400.0
            count = sum(1 for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip())
            out["deadline_audit"] = {"entries": count, "age_days": round(age, 1)}
        except Exception as exc:
            out["deadline_audit"] = {"error": str(exc)}
    else:
        out["deadline_audit"] = {"entries": 0, "age_days": None}

    out["warnings"] = warnings
    return out


# ---------------------------------------------------------------------------
# история §5 (S-07): метаданные state (meta) + экспорт/уничтожение данных дела
# ---------------------------------------------------------------------------


def set_meta(conn, key, value):
    """Одна строка в meta: используется для last_backup (S-85/S-86)."""
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (str(key), str(value))
    )
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
    if row is None:
        return default
    return row["value"]


#: Таблицы с case_id, которые трогает purge. Порядок фиксирован: превью и
#: отчёт должны быть детерминированы, а не зависеть обхода схемы.
PURGE_TABLES = (
    "events",
    "case_amounts",
    "case_documents",
    "case_notes",
    "deadline_states",
    "evidence_items",
    "case_representatives",
    "cases",
)


def case_row_counts(conn, case_id):
    """Счётчики строк дела по таблицам — то же число превью и факта purge."""
    counts = {}
    for table in PURGE_TABLES:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE case_id = ?", (case_id,)
        ).fetchone()
        counts[table] = int(row["n"])
    return counts


def case_file_paths(conn, case_id):
    """Физические файлы дела: только case_documents.path этого дела, unique.

    events.source_url не трогаем: это URL карточки, а не локальный файл, —
    «удалить» его значит выдумывать действие.
    """
    rows = conn.execute(
        "SELECT DISTINCT path FROM case_documents WHERE case_id = ? ORDER BY path ASC",
        (case_id,),
    ).fetchall()
    return [r["path"] for r in rows]


def scrub_deadline_audit(state_dir, case_id) -> int:
    """Убрать из журнала правок строки уничтоженного дела; вернуть их число.

    Журнал по обыкновению append-only, но purge — право субъекта (pdn_policy
    .subject_rights): оставить в audit строки дела с evidence-текстами после
    уничтожения означало бы, что «полное уничтожение данных дела» неполное.
    Файл переписывается атомарно (tmp+replace), чужие строки не трогаются.
    """
    from pathlib import Path

    p = Path(state_dir) / "deadline_audit.jsonl"
    if not p.exists():
        return 0
    kept, dropped = [], 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(obj, dict) and obj.get("case") == case_id:
            dropped += 1
            continue
        kept.append(line)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    tmp.replace(p)
    return dropped


def purge_case(conn, state_dir, case_id, actor="cli", remove_files=True):
    """Полное уничтожение данных дела (право субъекта, pdn_policy.subject_rights).

    Границы:
    - каскад строк ВСЕХ таблиц дела (PURGE_TABLES) + физфайлы из case_documents;
    - в audit попадает только ФАКТ: id/дата/actor/счётчики. Ни имён сторон, ни
      текстов документов, ни path'ов — ПДн в audit быть не должно;
    - подтверждения (--yes / confirm) отвечают вызывающему слою: здесь функция
      исполняет, а не спрашивает.
    """
    from pathlib import Path

    counts = case_row_counts(conn, case_id)
    paths = case_file_paths(conn, case_id)
    deleted_files, missing_files, failed_files, outside_files = [], [], [], []
    root = Path(state_dir).resolve()
    if remove_files:
        for raw in paths:
            p = Path(raw)
            try:
                rp = p.resolve()
            except OSError:
                failed_files.append(str(p))
                continue
            if not (rp == root or root in rp.parents):
                # containment: purge удаляет только файлы внутри state_dir (audit 25.10 #2)
                outside_files.append(str(p))
                continue
            try:
                if p.is_file():
                    p.unlink()
                    deleted_files.append(str(p))
                else:
                    missing_files.append(str(p))
            except OSError:
                failed_files.append(str(p))
    for table in PURGE_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,))
    conn.commit()
    # Строки журнала этого дела уходят вместе с делом: в purge-запись
    # попадает только счётчик сожжённых строк, а не их содержимое.
    scrubbed = scrub_deadline_audit(state_dir, case_id)
    entry = {
        "ts": _now_iso(),
        "actor": actor,
        "case": case_id,
        "action": "purge_case",
        "removed_rows": counts,
        "files_deleted": len(deleted_files),
        "files_missing": len(missing_files),
        "files_failed": len(failed_files),
        "files_outside_statedir": len(outside_files),
        "audit_lines_scrubbed": scrubbed,
    }
    append_deadline_audit(state_dir, entry)
    return {
        **entry,
        "files": {
            "deleted": len(deleted_files),
            "missing": len(missing_files),
            "failed": len(failed_files),
            "outside_statedir": len(outside_files),
        },
    }
