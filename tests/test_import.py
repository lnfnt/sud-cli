import json
from pathlib import Path

import pytest

from sud_cli.db import get_connection, init_db
from sud_cli.importing import import_fixture
from tests.conftest import run_cli


def make_court_fixture(records):
    return {"records": records}


def test_import_court_records_inserts_and_counts(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "court.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-0001",
                        "case_number": "CASE-42",
                        "court": "Synthetic District Court",
                        "parties": {"plaintiff": "Alice", "defendant": "Bob"},
                        "event_date": "2026-01-15",
                        "event_type": "hearing",
                        "doc_text": "Motion to compel discovery.",
                    }
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["inserted"] == 1

    conn = get_connection(tmp_path)
    rows = conn.execute("SELECT * FROM records").fetchall()
    assert len(rows) == 1
    assert rows[0]["record_id"] == "REC-0001"


def test_import_is_idempotent_by_record_id(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "court.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-0002",
                        "case_number": "CASE-99",
                        "court": "Synthetic Appeals Court",
                        "parties": {"plaintiff": "Corp A", "defendant": "Corp B"},
                        "event_date": "2026-02-20",
                        "event_type": "judgment",
                        "doc_text": "Final judgment text.",
                    }
                ]
            )
        )
    )
    run_cli(["--json", "import", "court", str(fixture)], env=env)
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["inserted"] == 0
    assert data["data"]["skipped"] == 1
    assert data["data"]["total"] == 1
    conn = get_connection(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_import_rejects_missing_record_id(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "bad.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "case_number": "CASE-X",
                        "court": "Synthetic Court",
                        "event_date": "2026-03-01",
                        "event_type": "hearing",
                    }
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code != 0
    assert "record_id" in out.lower() or "record_id" in err.lower()


def test_import_rejects_malformed_json(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "broken.json"
    fixture.write_text('{"records": [')
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code != 0


def test_import_rolls_back_on_invalid_record(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "mixed.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-OK",
                        "case_number": "CASE-OK",
                        "court": "Good Court",
                        "event_date": "2026-04-01",
                        "event_type": "hearing",
                    },
                    {
                        "record_id": "REC-BAD",
                        "case_number": "CASE-BAD",
                        "court": "Bad Court",
                        # missing event_type
                    },
                ]
            )
        )
    )
    run_cli(["--json", "import", "court", str(fixture)], env=env)
    conn = get_connection(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_import_respects_record_limit(tmp_path, monkeypatch):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    monkeypatch.setattr("sud_cli.importing.MAX_RECORDS", 2)
    fixture = tmp_path / "big.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": f"REC-{i:03d}",
                        "case_number": f"CASE-{i}",
                        "court": "Synthetic Court",
                        "event_date": "2026-01-01",
                        "event_type": "hearing",
                    }
                    for i in range(5)
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code != 0
    assert "limit" in out.lower() or "limit" in err.lower()


def test_import_rejects_directory_input(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "import", "court", str(tmp_path)], env=env)
    assert code != 0


def test_import_rejects_path_traversal(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "tricky.json"
    fixture.write_text("{}")
    # Pass a path that walks upward; product code should reject it.
    bad_path = str(tmp_path / ".." / "tricky.json")
    code, out, err = run_cli(["--json", "import", "court", bad_path], env=env)
    assert code != 0
    assert "traversal" in out.lower() or "traversal" in err.lower()


def test_import_rejects_invalid_utf8(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "utf8broken.json"
    fixture.write_bytes(b'{"records": [{"record_id": "R1", "case_number": "X", "court": "Y", "event_date": "2026-01-01", "event_type": "hearing", "doc_text": "\xff\xfe"}]}')
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code != 0
    assert "utf" in out.lower() or "utf" in err.lower() or "invalid" in out.lower()


def test_import_rejects_missing_file(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "import", "court", str(tmp_path / "missing.json")], env=env)
    assert code != 0
    assert "not found" in out.lower() or "not found" in err.lower()


def test_import_missing_file_error_does_not_expose_absolute_path(tmp_path, monkeypatch):
    """Missing input JSON errors must not leak the absolute file path or home directory."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("SUD_CLI_STATE_DIR", raising=False)
    missing = fake_home / "secret" / "court.json"
    missing.parent.mkdir()
    # Pass an absolute path that lives under the fake home to trigger the privacy defect.
    code, out, err = run_cli(["--json", "import", "court", str(missing)])
    assert code != 0
    combined = out + err
    assert str(fake_home) not in combined


def test_import_duplicate_with_different_content_is_skipped_not_overwritten(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "court.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-DUP",
                        "case_number": "CASE-ORIGINAL",
                        "court": "Synthetic District Court",
                        "event_date": "2026-01-15",
                        "event_type": "hearing",
                        "doc_text": "Original text.",
                    }
                ]
            )
        )
    )
    run_cli(["--json", "import", "court", str(fixture)], env=env)

    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-DUP",
                        "case_number": "CASE-CHANGED",
                        "court": "Changed Court",
                        "event_date": "2026-12-31",
                        "event_type": "judgment",
                        "doc_text": "Changed text.",
                    }
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["inserted"] == 0
    assert data["data"]["skipped"] == 1
    assert data["data"]["total"] == 1

    conn = get_connection(tmp_path)
    row = conn.execute("SELECT * FROM records WHERE record_id = ?", ("REC-DUP",)).fetchone()
    assert row["case_number"] == "CASE-ORIGINAL"
    assert row["court"] == "Synthetic District Court"
    assert row["doc_text"] == "Original text."


def test_import_court_duplicate_within_batch_counts_first_occurrence_wins(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "court_dup.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-DUP-BATCH",
                        "case_number": "CASE-FIRST",
                        "court": "First Court",
                        "event_date": "2026-01-01",
                        "event_type": "hearing",
                        "doc_text": "First text.",
                    },
                    {
                        "record_id": "REC-DUP-BATCH",
                        "case_number": "CASE-SECOND",
                        "court": "Second Court",
                        "event_date": "2026-02-02",
                        "event_type": "judgment",
                        "doc_text": "Second text.",
                    },
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["inserted"] == 1
    assert data["data"]["skipped"] == 1
    assert data["data"]["total"] == 2

    conn = get_connection(tmp_path)
    rows = conn.execute(
        "SELECT * FROM records WHERE record_id = ?", ("REC-DUP-BATCH",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["case_number"] == "CASE-FIRST"
    assert rows[0]["court"] == "First Court"
    assert rows[0]["doc_text"] == "First text."


def test_import_mixed_batch_skips_existing_and_inserts_new(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "court.json"
    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-EXISTING",
                        "case_number": "CASE-EXISTING",
                        "court": "Existing Court",
                        "event_date": "2026-01-15",
                        "event_type": "hearing",
                    }
                ]
            )
        )
    )
    run_cli(["--json", "import", "court", str(fixture)], env=env)

    fixture.write_text(
        json.dumps(
            make_court_fixture(
                [
                    {
                        "record_id": "REC-EXISTING",
                        "case_number": "CASE-CHANGED",
                        "court": "Changed Court",
                        "event_date": "2026-12-31",
                        "event_type": "judgment",
                    },
                    {
                        "record_id": "REC-NEW",
                        "case_number": "CASE-NEW",
                        "court": "New Court",
                        "event_date": "2026-06-01",
                        "event_type": "hearing",
                    },
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "court", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["data"]["inserted"] == 1
    assert data["data"]["skipped"] == 1
    assert data["data"]["total"] == 2

    conn = get_connection(tmp_path)
    rows = {row["record_id"]: dict(row) for row in conn.execute("SELECT * FROM records").fetchall()}
    assert len(rows) == 2
    assert rows["REC-EXISTING"]["case_number"] == "CASE-EXISTING"
    assert rows["REC-NEW"]["case_number"] == "CASE-NEW"
