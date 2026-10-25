import json
from pathlib import Path

import pytest

from tests.conftest import run_cli


def make_court_fixture(records):
    return {"records": records}


def import_court(tmp_path, env, records):
    fixture = tmp_path / "court.json"
    fixture.write_text(json.dumps(make_court_fixture(records)))
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err


def test_search_finds_record_by_text(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_court(
        tmp_path,
        env,
        [
            {
                "record_id": "REC-SEARCH-1",
                "case_number": "CASE-ALPHA",
                "court": "Synthetic District Court",
                "parties": {"plaintiff": "Alice", "defendant": "Bob"},
                "event_date": "2026-05-10",
                "event_type": "hearing",
                "doc_text": "Motion for summary judgment.",
            },
            {
                "record_id": "REC-SEARCH-2",
                "case_number": "CASE-BETA",
                "court": "Synthetic Appeals Court",
                "event_date": "2026-05-11",
                "event_type": "judgment",
                "doc_text": "Scheduling order.",
            },
        ],
    )
    code, out, err = run_cli(["--json", "search", "court", "summary"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 1
    assert data["data"][0]["record_id"] == "REC-SEARCH-1"


def test_search_no_matches_returns_empty_success(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_court(
        tmp_path,
        env,
        [
            {
                "record_id": "REC-SEARCH-3",
                "case_number": "CASE-GAMMA",
                "court": "Synthetic Court",
                "event_date": "2026-06-01",
                "event_type": "hearing",
                "doc_text": "Routine hearing.",
            }
        ],
    )
    code, out, err = run_cli(["--json", "search", "court", "nonexistent"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"] == []


def test_search_truncation_surfaced_when_results_exceed_limit(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_court(
        tmp_path,
        env,
        [
            {
                "record_id": f"REC-LIMIT-{i:03d}",
                "case_number": f"CASE-{i}",
                "court": "Synthetic Court",
                "event_date": "2026-01-01",
                "event_type": "hearing",
                "doc_text": "Motion to compel.",
            }
            for i in range(5)
        ],
    )
    code, out, err = run_cli(["--json", "search", "court", "compel", "--limit", "2"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 2
    assert data["meta"]["truncated"] is True
    assert data["meta"]["total"] == 5
    assert data["meta"]["limit"] == 2


def test_search_date_range_filter(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_court(
        tmp_path,
        env,
        [
            {
                "record_id": "REC-DATE-1",
                "case_number": "CASE-1",
                "court": "Synthetic Court",
                "event_date": "2026-01-10",
                "event_type": "hearing",
            },
            {
                "record_id": "REC-DATE-2",
                "case_number": "CASE-2",
                "court": "Synthetic Court",
                "event_date": "2026-03-10",
                "event_type": "hearing",
            },
        ],
    )
    code, out, err = run_cli(
        ["--json", "search", "court", "", "--since", "2026-02-01", "--until", "2026-12-31"],
        env=env,
    )
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    assert data["data"][0]["record_id"] == "REC-DATE-2"


def test_search_sql_injection_attempt_is_safe(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_court(
        tmp_path,
        env,
        [
            {
                "record_id": "REC-SQL-1",
                "case_number": "CASE-SQL",
                "court": "Synthetic Court",
                "event_date": "2026-07-01",
                "event_type": "hearing",
                "doc_text": "Motion.",
            }
        ],
    )
    code, out, err = run_cli(
        ["--json", "search", "court", "'; DROP TABLE records; --"], env=env
    )
    assert code == 0, err
    data = json.loads(out)
    assert data["data"] == []
    # Verify table still exists.
    code2, out2, err2 = run_cli(["--json", "search", "court", "Motion"], env=env)
    assert code2 == 0
    assert len(json.loads(out2)["data"]) == 1


def test_search_rejects_limit_above_maximum(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "search", "court", "x", "--limit", "9999"], env=env)
    assert code != 0
    assert "limit" in out.lower() or "limit" in err.lower()
