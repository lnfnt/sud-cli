"""Tests for the Phase 3 native court case/event timeline feature."""

import json
import sqlite3
from pathlib import Path

import pytest

from sud_cli.court_source import (
    STATUS_BLOCKED,
    STATUS_ERROR,
    STATUS_NO_RESULTS,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    CourtProviderResult,
    LocalJsonCourtSource,
)
from sud_cli.db import get_case_events, get_case_summary, get_connection, init_db
from sud_cli.importing import CourtImportError, import_court_cases
from tests.conftest import run_cli


def make_case_fixture(cases):
    return {"cases": cases}


def make_event(event_id, event_date, event_type="hearing", **kwargs):
    defaults = {
        "event_id": event_id,
        "event_date": event_date,
        "event_type": event_type,
        "doc_text": f"Synthetic {event_type}.",
        "source_name": "local-synthetic-json",
        "source_record_id": f"SOURCE-{event_id}",
        "fetched_at": "2026-01-01T10:00:00+00:00",
    }
    defaults.update(kwargs)
    return defaults


def make_case(case_id, events=None, **kwargs):
    defaults = {
        "case_id": case_id,
        "case_number": f"CASE-{case_id}",
        "court": "Synthetic Example Court",
        "parties": {"plaintiff": "Alpha Synthetic LLC", "defendant": "Beta Synthetic Inc."},
        "source_name": "local-synthetic-json",
        "source_record_id": f"SOURCE-{case_id}",
        "fetched_at": "2026-01-01T10:00:00+00:00",
        "source_url": "file:///fixtures/synthetic/court_cases.json",
    }
    defaults.update(kwargs)
    defaults["events"] = events if events is not None else []
    return defaults


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


def test_court_provider_statuses_are_typed_constants():
    assert STATUS_OK == "ok"
    assert STATUS_NO_RESULTS == "no_results"
    assert STATUS_UNAVAILABLE == "unavailable"
    assert STATUS_BLOCKED == "blocked"
    assert STATUS_ERROR == "error"


def test_court_provider_result_defaults():
    result = CourtProviderResult(status=STATUS_OK, items=[], source="test")
    assert result.status == "ok"
    assert result.pagination == {
        "page": 1,
        "page_size": 10,
        "total": 0,
        "pages_total": 0,
        "limit": 10,
    }
    assert result.error_message is None


def test_court_provider_result_no_results_status():
    result = CourtProviderResult(
        status=STATUS_NO_RESULTS,
        items=[],
        source="test",
        error_message="no matching cases",
    )
    assert result.status == "no_results"
    assert result.error_message == "no matching cases"


def test_court_provider_result_error_status_requires_message():
    # The dataclass does not enforce non-empty messages at runtime, but the
    # contract says error/unavailable/blocked must include one.
    result = CourtProviderResult(
        status=STATUS_ERROR, items=[], source="test", error_message="boom"
    )
    assert result.error_message == "boom"


def test_provider_result_distributes_statuses():
    ok = CourtProviderResult(status=STATUS_OK, items=[{"case_id": "x"}], source="s")
    no_results = CourtProviderResult(
        status=STATUS_NO_RESULTS, items=[], source="s", error_message="none"
    )
    unavailable = CourtProviderResult(
        status=STATUS_UNAVAILABLE, items=[], source="s", error_message="down"
    )
    blocked = CourtProviderResult(
        status=STATUS_BLOCKED, items=[], source="s", error_message="policy"
    )
    error = CourtProviderResult(
        status=STATUS_ERROR, items=[], source="s", error_message="fail"
    )
    assert ok.status == "ok" and ok.items
    assert no_results.status == "no_results" and no_results.items == []
    assert unavailable.status == "unavailable" and unavailable.error_message == "down"
    assert blocked.status == "blocked" and blocked.error_message == "policy"
    assert error.status == "error" and error.error_message == "fail"


# ---------------------------------------------------------------------------
# Local JSON court source
# ---------------------------------------------------------------------------


def test_local_source_imports_case_and_returns_ok(tmp_path):
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [make_case("SYNTH-CASE-0001", events=[make_event("E-1", "2026-01-10")])]
            )
        )
    )
    source = LocalJsonCourtSource()
    result = source.import_file(fixture)
    assert result.status == STATUS_OK
    assert len(result.items) == 1
    assert result.items[0]["case_id"] == "SYNTH-CASE-0001"
    assert result.items[0]["events"][0]["event_id"] == "E-1"


def test_local_source_returns_error_for_missing_file(tmp_path):
    source = LocalJsonCourtSource()
    result = source.import_file(tmp_path / "missing.json")
    assert result.status == STATUS_ERROR
    assert result.error_message
    assert result.items == []


def test_local_source_returns_error_for_malformed_json(tmp_path):
    fixture = tmp_path / "bad.json"
    fixture.write_text('{"cases": [')
    source = LocalJsonCourtSource()
    result = source.import_file(fixture)
    assert result.status == STATUS_ERROR
    assert "json" in result.error_message.lower()


def test_local_source_returns_no_results_for_empty_cases(tmp_path):
    fixture = tmp_path / "empty.json"
    fixture.write_text(json.dumps(make_case_fixture([])))
    source = LocalJsonCourtSource()
    result = source.import_file(fixture)
    assert result.status == STATUS_NO_RESULTS
    assert result.items == []


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def _create_v1_database(state_dir: Path):
    """Create a Phase 1 schema v1 database with one synthetic record."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "store.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE records (
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
        INSERT INTO meta (key, value) VALUES ('schema_version', '1');
        INSERT INTO records (record_id, case_number, court, plaintiff, defendant,
                             event_date, event_type, doc_text, imported_at)
        VALUES ('REC-V1-0001', 'CASE-V1-0001', 'Legacy Synthetic Court',
                'Alice', 'Bob', '2026-01-15', 'hearing',
                'Legacy record.', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()
    return db


def test_migration_from_v1_to_v2_preserves_records_and_creates_cases_events(tmp_path):
    _create_v1_database(tmp_path)
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert version == "3"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", ("CASE-V1-0001",)).fetchone()
    assert case is not None
    assert case["court"] == "Legacy Synthetic Court"
    assert case["plaintiff"] == "Alice"
    events = conn.execute("SELECT * FROM events WHERE case_id = ?", ("CASE-V1-0001",)).fetchall()
    assert len(events) == 1
    assert events[0]["event_type"] == "hearing"


def test_migration_idempotent(tmp_path):
    _create_v1_database(tmp_path)
    init_db(tmp_path)
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_migration_rollback_on_failure(tmp_path, monkeypatch):
    from sud_cli import db as db_module

    _create_v1_database(tmp_path)

    # Capture the exact pre-migration table set and the original records row.
    conn = sqlite3.connect(str(tmp_path / "store.db"))
    conn.row_factory = sqlite3.Row
    pre_tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    pre_record = conn.execute(
        "SELECT * FROM records WHERE record_id = ?", ("REC-V1-0001",)
    ).fetchone()
    pre_record_dict = dict(pre_record)
    conn.close()

    original = db_module._V1_TO_V2_MIGRATION
    # Inject a deliberately invalid statement to force the migration transaction
    # to fail.
    monkeypatch.setattr(
        db_module,
        "_V1_TO_V2_MIGRATION",
        original + ("THIS IS NOT VALID SQL;",),
    )
    with pytest.raises(Exception):
        init_db(tmp_path)

    conn = sqlite3.connect(str(tmp_path / "store.db"))
    conn.row_factory = sqlite3.Row
    version = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "1"

    post_tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # The pre-migration sqlite_master table set must be unchanged.
    assert post_tables == pre_tables
    # The original records row must survive unchanged.
    post_record = conn.execute(
        "SELECT * FROM records WHERE record_id = ?", ("REC-V1-0001",)
    ).fetchone()
    assert dict(post_record) == pre_record_dict
    # No partial Phase 3 schema must remain after rollback.
    assert "cases" not in post_tables
    assert "events" not in post_tables
    conn.close()


def test_unknown_schema_version_fails_closed(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = state_dir / "store.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '999');
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(Exception):
        init_db(state_dir)


# ---------------------------------------------------------------------------
# Native court import
# ---------------------------------------------------------------------------


def test_court_import_creates_case_and_events(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [
                    make_case(
                        "SYNTH-CASE-0001",
                        events=[
                            make_event("E-1", "2026-01-10", "hearing"),
                            make_event("E-2", "2026-01-20", "judgment"),
                        ],
                    )
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["inserted"] == 1
    assert data["data"]["events_inserted"] == 2

    conn = get_connection(tmp_path)
    case = get_case_summary(conn, "SYNTH-CASE-0001")
    assert case is not None
    assert case["case_number"] == "CASE-SYNTH-CASE-0001"
    events = get_case_events(conn, "SYNTH-CASE-0001")
    assert len(events) == 2


def test_court_import_idempotent_by_case_id(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [make_case("SYNTH-CASE-0002", events=[make_event("E-3", "2026-02-01")])]
            )
        )
    )
    run_cli(["--json", "court", "import", str(fixture)], env=env)
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["inserted"] == 0
    assert data["data"]["skipped"] == 1
    assert data["data"]["events_inserted"] == 0
    assert data["data"]["events_skipped"] == 1


def test_court_import_event_id_conflict_in_batch_skips_later(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [
                    make_case(
                        "SYNTH-CASE-0003",
                        events=[
                            make_event("E-DUP", "2026-03-01", "hearing", doc_text="first"),
                            make_event("E-DUP", "2026-03-02", "judgment", doc_text="second"),
                        ],
                    )
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["events_inserted"] == 1
    assert data["data"]["events_skipped"] == 1
    events = get_case_events(get_connection(tmp_path), "SYNTH-CASE-0003")
    assert len(events) == 1
    assert events[0]["event_type"] == "hearing"
    assert events[0]["doc_text"] == "first"


def test_court_import_duplicate_case_id_in_batch_skips_later(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [
                    make_case("SYNTH-CASE-DUP", events=[make_event("E-A", "2026-04-01")]),
                    make_case(
                        "SYNTH-CASE-DUP",
                        case_number="CHANGED-NUMBER",
                        events=[make_event("E-B", "2026-04-02")],
                    ),
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["inserted"] == 1
    assert data["data"]["skipped"] == 1
    case = get_case_summary(get_connection(tmp_path), "SYNTH-CASE-DUP")
    assert case["case_number"] != "CHANGED-NUMBER"
    events = get_case_events(get_connection(tmp_path), "SYNTH-CASE-DUP")
    assert len(events) == 1


def test_court_import_rejects_malformed_case_schema(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "bad.json"
    fixture.write_text(json.dumps(make_case_fixture([{"case_number": "x", "court": "y"}])))
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code != 0
    combined = out + err
    assert "case_id" in combined.lower()


def test_court_import_rejects_bad_source_url(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "bad.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [make_case("SYNTH-CASE-BAD-URL", source_url="ftp://evil.example.com/x")]
            )
        )
    )
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code != 0
    combined = out + err
    assert "url" in combined.lower() or "source_url" in combined.lower()


def test_court_import_rejects_path_traversal(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "tricky.json"
    fixture.write_text(json.dumps(make_case_fixture([make_case("X")])))
    bad_path = str(tmp_path / ".." / "tricky.json")
    code, out, err = run_cli(["--json", "court", "import", bad_path], env=env)
    assert code != 0
    combined = out + err
    assert "traversal" in combined.lower()


def test_court_import_rolls_back_on_bad_event(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "bad.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [
                    make_case(
                        "SYNTH-CASE-ROLLBACK",
                        events=[
                            make_event("E-GOOD", "2026-05-01"),
                            {"event_id": "E-BAD", "event_type": "hearing"},  # missing source_name
                        ],
                    )
                ]
            )
        )
    )
    run_cli(["--json", "court", "import", str(fixture)], env=env)
    conn = get_connection(tmp_path)
    assert get_case_summary(conn, "SYNTH-CASE-ROLLBACK") is None
    assert conn.execute("SELECT COUNT(*) FROM events WHERE case_id = ?", ("SYNTH-CASE-ROLLBACK",)).fetchone()[0] == 0


def test_court_import_respects_case_limit(tmp_path, monkeypatch):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    monkeypatch.setattr("sud_cli.importing.MAX_CASES", 2)
    fixture = tmp_path / "big.json"
    fixture.write_text(
        json.dumps(make_case_fixture([make_case(f"CASE-{i:03d}") for i in range(5)]))
    )
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code != 0
    assert "limit" in (out + err).lower()


# ---------------------------------------------------------------------------
# Case view / timeline
# ---------------------------------------------------------------------------


def test_court_case_returns_summary_and_ordered_timeline(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [
                    make_case(
                        "SYNTH-CASE-VIEW",
                        events=[
                            make_event("E-LATE", "2026-06-20"),
                            make_event("E-EARLY", "2026-06-10"),
                        ],
                    )
                ]
            )
        )
    )
    run_cli(["--json", "court", "import", str(fixture)], env=env)
    code, out, err = run_cli(["--json", "court", "case", "SYNTH-CASE-VIEW"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    summary = data["data"]
    assert summary["case_id"] == "SYNTH-CASE-VIEW"
    assert summary["court"] == "Synthetic Example Court"
    event_ids = [e["event_id"] for e in summary["events"]]
    assert event_ids == ["E-EARLY", "E-LATE"]


def test_court_case_deterministic_order_for_same_event_date(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(
        json.dumps(
            make_case_fixture(
                [
                    make_case(
                        "SYNTH-CASE-TIE",
                        events=[
                            make_event("E-Z", "2026-07-01"),
                            make_event("E-A", "2026-07-01"),
                            make_event("E-M", "2026-07-01"),
                        ],
                    )
                ]
            )
        )
    )
    run_cli(["--json", "court", "import", str(fixture)], env=env)
    code, out, err = run_cli(["--json", "court", "case", "SYNTH-CASE-TIE"], env=env)
    assert code == 0, err
    event_ids = [e["event_id"] for e in json.loads(out)["data"]["events"]]
    assert event_ids == ["E-A", "E-M", "E-Z"]


def test_court_case_no_events_vs_unknown_case(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps(make_case_fixture([make_case("SYNTH-CASE-EMPTY")])))
    run_cli(["--json", "court", "import", str(fixture)], env=env)
    code, out, err = run_cli(["--json", "court", "case", "SYNTH-CASE-EMPTY"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["events"] == []
    assert data["data"]["event_count"] == 0

    code, out, err = run_cli(["--json", "court", "case", "SYNTH-CASE-UNKNOWN"], env=env)
    assert code != 0
    combined = out + err
    assert "not found" in combined.lower()


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


def test_legacy_import_court_still_works_after_migration(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "legacy.json"
    fixture.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "record_id": "LEGACY-REC-0001",
                        "case_number": "LEGACY-CASE-0001",
                        "court": "Legacy Synthetic Court",
                        "event_date": "2026-08-01",
                        "event_type": "hearing",
                    }
                ]
            }
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["inserted"] == 1
    # The migrated case should also be queryable by the native case command.
    code, out, err = run_cli(["--json", "court", "case", "LEGACY-CASE-0001"], env=env)
    assert code == 0, err
    summary = json.loads(out)["data"]
    assert summary["case_id"] == "LEGACY-CASE-0001"
    assert len(summary["events"]) == 1


def test_search_court_still_works_after_migration(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "legacy.json"
    fixture.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "record_id": "SEARCH-REC-0001",
                        "case_number": "SEARCH-CASE-0001",
                        "court": "Search Synthetic Court",
                        "event_date": "2026-09-01",
                        "event_type": "hearing",
                        "doc_text": "Searchable text.",
                    }
                ]
            }
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    code, out, err = run_cli(["--json", "search", "court", "Searchable"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    assert data["data"][0]["record_id"] == "SEARCH-REC-0001"


# ---------------------------------------------------------------------------
# Calculators stay fail-closed
# ---------------------------------------------------------------------------
# Since spec v1.0.0 (approved 2026-10-08) fees/deadlines are live, so these
# assert the surviving contract: an invocation that omits a legally-required
# parameter is refused, whatever the argument order, and never answers with a
# number or date.


@pytest.mark.parametrize(
    "args",
    [
        ["fees"],
        ["--json", "fees", "--court", "district"],
        ["deadlines", "2026-01-01", "--appeal"],
        ["--json", "deadlines", "--service-method", "mail"],
        ["deadlines", "as_appeal_decision"],
        ["fees", "--jurisdiction", "arbitration"],
    ],
)
def test_incomplete_calculators_stay_fail_closed_with_various_argument_orders(args):
    code, out, err = run_cli(args)
    assert code != 0
    combined = out + err
    assert "amount_rub" not in combined
    assert '"deadline"' not in combined


def test_incomplete_fees_with_state_dir_before_command_is_fail_closed(tmp_path):
    """A pre-command --state-dir must not let a bad fees call slip through."""
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(
        ["--state-dir", str(tmp_path), "--json", "fees", "--amount", "10000"], env=env
    )
    assert code != 0
    data = json.loads(err)  # JSON-mode errors go to stderr by convention
    assert data["status"] in {"blocked", "error"}
    assert "amount_rub" not in out + err
