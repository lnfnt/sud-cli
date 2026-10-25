"""Tests for the Phase 4 native offline court search feature."""

import json
import sqlite3
from pathlib import Path

import pytest

from sud_cli.db import SearchError, get_connection, init_db, search_court_cases
from sud_cli.importing import import_court_cases
from tests.conftest import run_cli


def make_case_fixture(cases):
    return {"cases": cases}


def make_event(event_id, event_date=None, event_type="hearing", doc_text=None, **kwargs):
    defaults = {
        "event_id": event_id,
        "event_date": event_date,
        "event_type": event_type,
        "doc_text": doc_text if doc_text is not None else f"Synthetic {event_type}.",
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


def import_cases(tmp_path, env, cases):
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps(make_case_fixture(cases)))
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=env)
    assert code == 0, err


# ---------------------------------------------------------------------------
# Native search function unit tests
# ---------------------------------------------------------------------------


def test_search_matches_case_metadata_only(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-META",
                court="Unique Synthetic Tribunal",
                events=[make_event("E-META", "2026-01-10", "hearing", doc_text="routine")],
            ),
            make_case(
                "SYNTH-CASE-OTHER",
                events=[make_event("E-OTHER", "2026-01-11", "hearing")],
            ),
        ],
    )
    code, out, err = run_cli(["--json", "court", "search", "Tribunal"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 1
    assert data["data"][0]["case_id"] == "SYNTH-CASE-META"
    # The case matched by metadata; its single event did not match the query.
    assert data["data"][0]["matching_event_count"] == 0
    assert data["data"][0]["matching_events"] == []


def test_search_matches_events_and_groups_distinct_cases(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-A",
                events=[
                    make_event("E-A1", "2026-02-10", "motion", doc_text="widget dispute"),
                    make_event("E-A2", "2026-02-11", "hearing", doc_text="widget dispute hearing"),
                ],
            ),
            make_case(
                "SYNTH-CASE-B",
                events=[make_event("E-B1", "2026-03-01", "judgment", doc_text="widget ruling")],
            ),
            make_case(
                "SYNTH-CASE-C",
                events=[make_event("E-C1", "2026-03-02", "hearing", doc_text="unrelated matter")],
            ),
        ],
    )
    code, out, err = run_cli(["--json", "court", "search", "widget"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 2
    case_ids = {item["case_id"] for item in data["data"]}
    assert case_ids == {"SYNTH-CASE-A", "SYNTH-CASE-B"}
    a_item = next(item for item in data["data"] if item["case_id"] == "SYNTH-CASE-A")
    assert a_item["matching_event_count"] == 2
    b_item = next(item for item in data["data"] if item["case_id"] == "SYNTH-CASE-B")
    assert b_item["matching_event_count"] == 1


def test_search_no_matches_returns_empty_success(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(tmp_path, env, [make_case("SYNTH-CASE-NO")])
    code, out, err = run_cli(["--json", "court", "search", "nonexistent-query"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"] == []
    assert data["meta"]["total"] == 0
    assert data["meta"]["truncated"] is False


def test_search_query_error_propagates(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(tmp_path, env, [make_case("SYNTH-CASE-ERR")])
    code, out, err = run_cli(["--json", "court", "search", "x", "--limit", "0"], env=env)
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"


def test_search_inclusive_date_range(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-EARLY",
                events=[make_event("E-EARLY", "2026-01-10", "hearing", doc_text="widget motion")],
            ),
            make_case(
                "SYNTH-CASE-LATE",
                events=[make_event("E-LATE", "2026-06-15", "hearing", doc_text="widget motion")],
            ),
        ],
    )
    code, out, err = run_cli(
        ["--json", "court", "search", "widget", "--since", "2026-02-01", "--until", "2026-12-31"],
        env=env,
    )
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    assert data["data"][0]["case_id"] == "SYNTH-CASE-LATE"
    assert data["data"][0]["matching_event_count"] == 1


def test_search_missing_event_dates_ordered_last(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-DATED",
                events=[make_event("E-DATED", "2026-05-01", "hearing", doc_text="widget")],
            ),
            make_case(
                "SYNTH-CASE-UNDATED",
                events=[make_event("E-UNDATED", None, "hearing", doc_text="widget")],
            ),
        ],
    )
    code, out, err = run_cli(["--json", "court", "search", "widget"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert [item["case_id"] for item in data["data"]] == [
        "SYNTH-CASE-DATED",
        "SYNTH-CASE-UNDATED",
    ]


def test_search_rejects_malformed_date(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(
        ["--json", "court", "search", "x", "--since", "not-a-date"], env=env
    )
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"
    assert "YYYY-MM-DD" in data["data"]


def test_search_rejects_reversed_date_range(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(
        [
            "--json",
            "court",
            "search",
            "x",
            "--since",
            "2026-12-31",
            "--until",
            "2026-01-01",
        ],
        env=env,
    )
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"
    assert "since" in data["data"].lower() and "until" in data["data"].lower()


def test_search_sql_injection_attempt_is_safe(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [make_case("SYNTH-CASE-SQL", events=[make_event("E-SQL", "2026-07-01", "hearing")])],
    )
    code, out, err = run_cli(
        ["--json", "court", "search", "'; DROP TABLE cases; --"], env=env
    )
    assert code == 0, err
    data = json.loads(out)
    assert data["data"] == []
    # Verify tables still exist.
    code2, out2, err2 = run_cli(["--json", "court", "search", "hearing"], env=env)
    assert code2 == 0, err2
    assert len(json.loads(out2)["data"]) == 1


def test_search_like_wildcards_are_literal(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-PERCENT",
                case_number="CASE-100%",
                events=[make_event("E-P", "2026-08-01", "hearing")],
            ),
            make_case(
                "SYNTH-CASE-UNDERSCORE",
                case_number="CASE_2026",
                events=[make_event("E-U", "2026-08-02", "hearing")],
            ),
        ],
    )
    # '%' as a wildcard would match everything; escaped it only matches a
    # literal percent sign, so the underscore-only record is not returned.
    code, out, err = run_cli(["--json", "court", "search", "%"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    assert data["data"][0]["case_id"] == "SYNTH-CASE-PERCENT"

    # '_' as a wildcard would match any single character; escaped it only
    # matches a literal underscore, so the percent-only record is not returned.
    code, out, err = run_cli(["--json", "court", "search", "_"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    assert data["data"][0]["case_id"] == "SYNTH-CASE-UNDERSCORE"

    # Exact literal substring matches should work.
    code, out, err = run_cli(["--json", "court", "search", "100%"], env=env)
    assert code == 0, err
    assert len(json.loads(out)["data"]) == 1


def test_search_deterministic_order_for_tied_dates(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-Z",
                events=[make_event("E-Z", "2026-09-01", "hearing", doc_text="widget")],
            ),
            make_case(
                "SYNTH-CASE-A",
                events=[make_event("E-A", "2026-09-01", "hearing", doc_text="widget")],
            ),
            make_case(
                "SYNTH-CASE-M",
                events=[make_event("E-M", "2026-09-01", "hearing", doc_text="widget")],
            ),
        ],
    )
    code, out, err = run_cli(["--json", "court", "search", "widget"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert [item["case_id"] for item in data["data"]] == [
        "SYNTH-CASE-A",
        "SYNTH-CASE-M",
        "SYNTH-CASE-Z",
    ]


def test_search_limit_total_and_truncation(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                f"SYNTH-CASE-LIMIT-{i:03d}",
                events=[
                    make_event(
                        f"E-LIMIT-{i:03d}",
                        f"2026-{i + 1:02d}-01",
                        "hearing",
                        doc_text="widget motion",
                    )
                ],
            )
            for i in range(5)
        ],
    )
    code, out, err = run_cli(
        ["--json", "court", "search", "widget", "--limit", "2"], env=env
    )
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 2
    assert data["meta"]["limit"] == 2
    assert data["meta"]["total"] == 5
    assert data["meta"]["truncated"] is True


def test_search_does_not_leak_full_document_body(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    long_text = "WIDGET " + ("x" * 5000)
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-LONG",
                events=[make_event("E-LONG", "2026-10-01", "hearing", doc_text=long_text)],
            )
        ],
    )
    code, out, err = run_cli(["--json", "court", "search", "WIDGET"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    event = data["data"][0]["matching_events"][0]
    assert "doc_text" not in event
    assert "snippet" in event
    assert len(event["snippet"]) <= 200
    assert "widget" in event["snippet"].lower() or "WIDGET" in event["snippet"]
    assert long_text not in out


def test_search_empty_query_allowed_with_date_bound(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [
            make_case(
                "SYNTH-CASE-DATEBOUND",
                events=[make_event("E-DATEBOUND", "2026-11-01", "hearing")],
            )
        ],
    )
    code, out, err = run_cli(
        ["--json", "court", "search", "", "--since", "2026-10-01"], env=env
    )
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1


def test_search_empty_query_rejected_without_filters(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "court", "search", ""], env=env)
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"


def test_search_rejects_limit_above_maximum(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(
        ["--json", "court", "search", "x", "--limit", "9999"], env=env
    )
    assert code != 0
    data = json.loads(err)
    assert data["status"] == "error"
    assert "limit" in data["data"].lower()


def test_search_function_limit_validation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with pytest.raises(SearchError):
        search_court_cases(conn, query="x", limit=0)
    with pytest.raises(SearchError):
        search_court_cases(conn, query="x", limit=200)


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


def test_legacy_search_court_still_works_after_phase4(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "legacy.json"
    fixture.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "record_id": "LEGACY-SEARCH-0001",
                        "case_number": "LEGACY-CASE-0001",
                        "court": "Legacy Synthetic Court",
                        "event_date": "2026-09-01",
                        "event_type": "hearing",
                        "doc_text": "Legacy searchable text.",
                    }
                ]
            }
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    code, out, err = run_cli(["--json", "search", "court", "Legacy"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert len(data["data"]) == 1
    assert data["data"][0]["record_id"] == "LEGACY-SEARCH-0001"


def test_native_case_command_still_works_after_phase4(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_cases(
        tmp_path,
        env,
        [make_case("SYNTH-CASE-REGRESSION", events=[make_event("E-REG", "2026-12-01")])],
    )
    code, out, err = run_cli(["--json", "court", "case", "SYNTH-CASE-REGRESSION"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["case_id"] == "SYNTH-CASE-REGRESSION"


@pytest.mark.parametrize(
    "args",
    [
        ["fees"],
        ["--json", "fees", "--amount", "10000"],
        ["deadlines", "2026-01-01", "--appeal"],
        ["--json", "deadlines", "--service-method", "mail"],
        ["deadlines", "as_appeal_decision"],
        ["fees", "--kind", "appeal"],
    ],
)
def test_incomplete_calculators_stay_fail_closed_with_various_argument_orders(args):
    """Since spec v1.0.0 these calculators are live, but still fail closed on
    an invocation that omits a legally-required parameter."""
    code, out, err = run_cli(args)
    assert code != 0
    combined = out + err
    assert "amount_rub" not in combined
    assert '"deadline"' not in combined
