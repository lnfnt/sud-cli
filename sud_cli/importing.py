"""Local JSON import pipeline for synthetic court and NPA fixtures."""

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

MAX_RECORDS = 1000
MAX_CASES = 1000
MAX_EVENTS = 1000
MAX_TEXT_LENGTH = 1_000_000
MAX_FILE_BYTES = 10 * 1024 * 1024


class ImportError(Exception):
    """Raised when a fixture cannot be imported safely."""


class CourtImportError(Exception):
    """Raised when a native court case/event fixture cannot be imported safely."""


COURT_REQUIRED_FIELDS = {
    "record_id": str,
    "case_number": str,
    "court": str,
    "event_date": str,
    "event_type": str,
}

NPA_REQUIRED_FIELDS = {
    "doc_id": str,
    "edition": str,
    "title": str,
}


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).replace(microsecond=0).isoformat()


def _validate_path(file_path: Path) -> Path:
    if file_path.is_dir():
        raise ImportError("input path is a directory, not a file")
    if ".." in file_path.parts:
        raise ImportError("path traversal is not allowed")
    if not file_path.exists():
        raise ImportError("input file not found")
    if not file_path.is_file():
        raise ImportError("input path is not a regular file")
    if file_path.stat().st_size > MAX_FILE_BYTES:
        raise ImportError(f"file exceeds maximum size of {MAX_FILE_BYTES} bytes")
    return file_path


def _load_json(file_path: Path) -> Any:
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ImportError(f"invalid UTF-8: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError(f"malformed JSON: {exc}") from exc


def _check_text_length(value: str, name: str) -> None:
    if len(value) > MAX_TEXT_LENGTH:
        raise ImportError(f"field '{name}' exceeds maximum length of {MAX_TEXT_LENGTH}")


def _validate_iso_date(value: str, field: str) -> None:
    try:
        datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ImportError(f"field '{field}' must be YYYY-MM-DD: {value}") from exc


def validate_court_record(record: Any, index: int) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ImportError(f"record {index} is not an object")
    for field, expected_type in COURT_REQUIRED_FIELDS.items():
        if field not in record:
            raise ImportError(f"record {index} missing required field '{field}'")
        value = record[field]
        if not isinstance(value, expected_type):
            raise ImportError(
                f"record {index} field '{field}' must be {expected_type.__name__}"
            )
        _check_text_length(value, field)
    _validate_iso_date(record["event_date"], "event_date")

    parties = record.get("parties", {})
    if not isinstance(parties, dict):
        raise ImportError(f"record {index} 'parties' must be an object")

    return {
        "record_id": record["record_id"],
        "case_number": record["case_number"],
        "court": record["court"],
        "plaintiff": str(parties.get("plaintiff", "")),
        "defendant": str(parties.get("defendant", "")),
        "event_date": record["event_date"],
        "event_type": record["event_type"],
        "doc_text": str(record.get("doc_text", "")),
    }


def validate_npa_document(doc: Any, index: int) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise ImportError(f"document {index} is not an object")
    for field, expected_type in NPA_REQUIRED_FIELDS.items():
        if field not in doc:
            raise ImportError(f"document {index} missing required field '{field}'")
        value = doc[field]
        if not isinstance(value, expected_type):
            raise ImportError(
                f"document {index} field '{field}' must be {expected_type.__name__}"
            )
        _check_text_length(value, field)
    if "doc_date" in doc:
        _validate_iso_date(doc["doc_date"], "doc_date")
    _check_text_length(str(doc.get("body", "")), "body")
    return {
        "doc_id": doc["doc_id"],
        "edition": doc["edition"],
        "title": doc["title"],
        "doc_date": doc.get("doc_date"),
        "body": str(doc.get("body", "")),
    }


def _prepare_court_records(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        raise ImportError("top-level JSON value must be an object")
    if "records" not in data:
        raise ImportError("top-level object must contain a 'records' array")
    records = data["records"]
    if not isinstance(records, list):
        raise ImportError("'records' must be an array")
    if len(records) > MAX_RECORDS:
        raise ImportError(
            f"import limit exceeded: {len(records)} records (maximum {MAX_RECORDS})"
        )
    return [validate_court_record(r, i) for i, r in enumerate(records)]


def _prepare_npa_documents(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        raise ImportError("top-level JSON value must be an object")
    if "documents" not in data:
        raise ImportError("top-level object must contain a 'documents' array")
    documents = data["documents"]
    if not isinstance(documents, list):
        raise ImportError("'documents' must be an array")
    if len(documents) > MAX_RECORDS:
        raise ImportError(
            f"import limit exceeded: {len(documents)} documents (maximum {MAX_RECORDS})"
        )
    return [validate_npa_document(d, i) for i, d in enumerate(documents)]


def _existing_record_ids(conn: sqlite3.Connection, ids: List[str]) -> set:
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT record_id FROM records WHERE record_id IN ({placeholders})", ids
    ).fetchall()
    return {row["record_id"] for row in rows}


def _existing_npa_keys(conn: sqlite3.Connection, keys: List[tuple]) -> set:
    if not keys:
        return set()
    # SQLite supports row values for IN.
    placeholders = ",".join("(?, ?)" for _ in keys)
    flat = [item for pair in keys for item in pair]
    rows = conn.execute(
        f"SELECT doc_id, edition FROM npa_docs WHERE (doc_id, edition) IN ({placeholders})",
        flat,
    ).fetchall()
    return {(row["doc_id"], row["edition"]) for row in rows}


def import_fixture(conn: sqlite3.Connection, kind: str, file_path: Path) -> Dict[str, Any]:
    file_path = _validate_path(file_path)
    data = _load_json(file_path)

    imported_at = _now_iso()

    with conn:
        if kind == "court":
            records = _prepare_court_records(data)
            ids = [r["record_id"] for r in records]
            existing = _existing_record_ids(conn, ids)
            inserted = 0
            skipped = 0
            seen = set()
            for r in records:
                key = r["record_id"]
                if key in existing or key in seen:
                    skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO records
                    (record_id, case_number, court, plaintiff, defendant,
                     event_date, event_type, doc_text, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["record_id"],
                        r["case_number"],
                        r["court"],
                        r["plaintiff"],
                        r["defendant"],
                        r["event_date"],
                        r["event_type"],
                        r["doc_text"],
                        imported_at,
                    ),
                )
                # Keep the native case/event model in sync with legacy records.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cases
                    (case_id, case_number, court, plaintiff, defendant,
                     source_name, source_record_id, fetched_at, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["case_number"],
                        r["case_number"],
                        r["court"],
                        r["plaintiff"],
                        r["defendant"],
                        "local-json-court",
                        r["record_id"],
                        imported_at,
                        imported_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO events
                    (event_id, case_id, event_date, event_type, doc_text,
                     source_name, source_record_id, fetched_at, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["record_id"],
                        r["case_number"],
                        r["event_date"],
                        r["event_type"],
                        r["doc_text"],
                        "local-json-court",
                        r["record_id"],
                        imported_at,
                        imported_at,
                    ),
                )
                seen.add(key)
                inserted += 1
            return {"kind": "court", "inserted": inserted, "skipped": skipped, "total": len(records)}

        elif kind == "npa":
            docs = _prepare_npa_documents(data)
            keys = [(d["doc_id"], d["edition"]) for d in docs]
            existing = _existing_npa_keys(conn, keys)
            inserted = 0
            skipped = 0
            seen = set()
            for d in docs:
                key = (d["doc_id"], d["edition"])
                if key in existing or key in seen:
                    skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO npa_docs
                    (doc_id, edition, title, doc_date, body, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        d["doc_id"],
                        d["edition"],
                        d["title"],
                        d["doc_date"],
                        d["body"],
                        imported_at,
                    ),
                )
                seen.add(key)
                inserted += 1
            return {"kind": "npa", "inserted": inserted, "skipped": skipped, "total": len(docs)}

        else:
            raise ImportError(f"unsupported import kind: {kind}")


# ---------------------------------------------------------------------------
# Native court case/event import (Phase 3)
# ---------------------------------------------------------------------------

COURT_CASE_REQUIRED_FIELDS = {
    "case_id": str,
    "case_number": str,
    "court": str,
    "source_name": str,
}

COURT_EVENT_REQUIRED_FIELDS = {
    "event_id": str,
    "event_type": str,
    "source_name": str,
    "source_record_id": str,
}

ALLOWED_SOURCE_SCHEMES = {"http", "https", "file"}


def _validate_datetime(value: str, field: str) -> None:
    try:
        datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CourtImportError(f"field '{field}' must be ISO-8601 datetime: {value}") from exc


def _validate_source_url(value: Any, record_name: str) -> None:
    if not isinstance(value, str):
        raise CourtImportError(f"{record_name} 'source_url' must be a string")
    if ".." in value:
        raise CourtImportError(f"{record_name} 'source_url' contains path traversal")
    if any(ord(ch) < 32 for ch in value):
        raise CourtImportError(f"{record_name} 'source_url' contains control characters")
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme not in ALLOWED_SOURCE_SCHEMES:
        raise CourtImportError(
            f"{record_name} 'source_url' uses unsupported scheme {parsed.scheme!r}"
        )


def validate_court_case(case: Any, index: int) -> Dict[str, Any]:
    if not isinstance(case, dict):
        raise CourtImportError(f"case {index} is not an object")
    for field, expected_type in COURT_CASE_REQUIRED_FIELDS.items():
        if field not in case:
            raise CourtImportError(f"case {index} missing required field '{field}'")
        value = case[field]
        if not isinstance(value, expected_type):
            raise CourtImportError(
                f"case {index} field '{field}' must be {expected_type.__name__}"
            )
        _check_text_length(value, field)

    parties = case.get("parties", {})
    if not isinstance(parties, dict):
        raise CourtImportError(f"case {index} 'parties' must be an object")

    source_url = case.get("source_url")
    if source_url is not None:
        _validate_source_url(source_url, f"case {index}")
    fetched_at = case.get("fetched_at")
    if fetched_at is not None:
        _validate_datetime(fetched_at, "fetched_at")

    source_record_id = case.get("source_record_id")
    if source_record_id is not None and not isinstance(source_record_id, str):
        raise CourtImportError(f"case {index} 'source_record_id' must be a string")

    return {
        "case_id": case["case_id"],
        "case_number": case["case_number"],
        "court": case["court"],
        "plaintiff": str(parties.get("plaintiff", "")),
        "defendant": str(parties.get("defendant", "")),
        "source_name": case["source_name"],
        "source_record_id": str(source_record_id) if source_record_id is not None else None,
        "fetched_at": fetched_at,
        "source_url": source_url,
    }


def validate_court_event(event: Any, index: int, case_index: int) -> Dict[str, Any]:
    if not isinstance(event, dict):
        raise CourtImportError(f"event {index} of case {case_index} is not an object")
    for field, expected_type in COURT_EVENT_REQUIRED_FIELDS.items():
        if field not in event:
            raise CourtImportError(
                f"event {index} of case {case_index} missing required field '{field}'"
            )
        value = event[field]
        if not isinstance(value, expected_type):
            raise CourtImportError(
                f"event {index} of case {case_index} field '{field}' must be {expected_type.__name__}"
            )
        _check_text_length(value, field)

    event_date = event.get("event_date")
    if event_date is not None:
        _validate_iso_date(event_date, "event_date")
    fetched_at = event.get("fetched_at")
    if fetched_at is not None:
        _validate_datetime(fetched_at, "fetched_at")
    source_url = event.get("source_url")
    if source_url is not None:
        _validate_source_url(source_url, f"event {index} of case {case_index}")

    doc_text = str(event.get("doc_text", ""))
    _check_text_length(doc_text, "doc_text")

    return {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "event_date": event_date,
        "doc_text": doc_text,
        "source_name": event["source_name"],
        "source_record_id": event["source_record_id"],
        "fetched_at": fetched_at,
        "source_url": source_url,
    }


def _prepare_court_cases(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        raise CourtImportError("top-level JSON value must be an object")
    if "cases" not in data:
        raise CourtImportError("top-level object must contain a 'cases' array")
    cases = data["cases"]
    if not isinstance(cases, list):
        raise CourtImportError("'cases' must be an array")
    if len(cases) > MAX_CASES:
        raise CourtImportError(
            f"import limit exceeded: {len(cases)} cases (maximum {MAX_CASES})"
        )

    total_events = 0
    normalized = []
    for i, case in enumerate(cases):
        normalized_case = validate_court_case(case, i)
        events = case.get("events", [])
        if not isinstance(events, list):
            raise CourtImportError(f"case {i} 'events' must be an array")
        if len(events) > MAX_EVENTS:
            raise CourtImportError(
                f"case {i} event limit exceeded: {len(events)} events (maximum {MAX_EVENTS})"
            )
        total_events += len(events)
        if total_events > MAX_EVENTS:
            raise CourtImportError(
                f"import event limit exceeded: {total_events} events (maximum {MAX_EVENTS})"
            )
        normalized_case["events"] = [
            validate_court_event(e, j, i) for j, e in enumerate(events)
        ]
        normalized.append(normalized_case)
    return normalized


def load_court_case_fixture(file_path: Path) -> List[Dict[str, Any]]:
    file_path = _validate_path(file_path)
    data = _load_json(file_path)
    return _prepare_court_cases(data)


def _existing_case_ids(conn: sqlite3.Connection, ids: List[str]) -> set:
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT case_id FROM cases WHERE case_id IN ({placeholders})", ids
    ).fetchall()
    return {row["case_id"] for row in rows}


def _existing_event_ids(conn: sqlite3.Connection, ids: List[str]) -> set:
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT event_id FROM events WHERE event_id IN ({placeholders})", ids
    ).fetchall()
    return {row["event_id"] for row in rows}


def import_court_cases(conn: sqlite3.Connection, file_path: Path) -> Dict[str, Any]:
    cases = load_court_case_fixture(file_path)
    imported_at = _now_iso()

    total_events = sum(len(c.get("events", [])) for c in cases)
    case_ids = [c["case_id"] for c in cases]
    event_ids = [e["event_id"] for c in cases for e in c.get("events", [])]

    with conn:
        existing_cases = _existing_case_ids(conn, case_ids)
        existing_events = _existing_event_ids(conn, event_ids)

        inserted = 0
        skipped = 0
        events_inserted = 0
        events_skipped = 0
        seen_cases: set = set()
        seen_events: set = set()

        for case in cases:
            case_id = case["case_id"]
            if case_id in existing_cases or case_id in seen_cases:
                skipped += 1
                events_skipped += len(case.get("events", []))
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO cases
                (case_id, case_number, court, plaintiff, defendant,
                 source_name, source_record_id, fetched_at, source_url, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    case["case_number"],
                    case["court"],
                    case["plaintiff"],
                    case["defendant"],
                    case["source_name"],
                    case["source_record_id"],
                    case["fetched_at"],
                    case["source_url"],
                    imported_at,
                ),
            )
            seen_cases.add(case_id)
            inserted += 1

            for event in case.get("events", []):
                event_id = event["event_id"]
                if event_id in existing_events or event_id in seen_events:
                    events_skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO events
                    (event_id, case_id, event_date, event_type, doc_text,
                     source_name, source_record_id, fetched_at, source_url, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        case_id,
                        event["event_date"],
                        event["event_type"],
                        event["doc_text"],
                        event["source_name"],
                        event["source_record_id"],
                        event["fetched_at"],
                        event["source_url"],
                        imported_at,
                    ),
                )
                seen_events.add(event_id)
                events_inserted += 1

    return {
        "kind": "court-case",
        "inserted": inserted,
        "skipped": skipped,
        "events_inserted": events_inserted,
        "events_skipped": events_skipped,
        "total_cases": len(cases),
        "total_events": total_events,
    }
