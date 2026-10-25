import json
from pathlib import Path

from sud_cli.db import get_connection
from tests.conftest import run_cli


def make_npa_fixture(documents):
    return {"documents": documents}


def import_npa(tmp_path, env, documents):
    fixture = tmp_path / "npa.json"
    fixture.write_text(json.dumps(make_npa_fixture(documents)))
    code, out, err = run_cli(["--json", "import", "npa", str(fixture)], env=env)
    assert code == 0, err


def test_import_npa_documents(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_npa(
        tmp_path,
        env,
        [
            {
                "doc_id": "SYNTH-001",
                "edition": "v1",
                "title": "Synthetic Regulation on Widgets",
                "doc_date": "2026-01-01",
                "body": "Original text of the synthetic regulation.",
            }
        ],
    )


def test_npa_search_finds_by_title(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_npa(
        tmp_path,
        env,
        [
            {
                "doc_id": "SYNTH-002",
                "edition": "v1",
                "title": "Synthetic Environmental Rule",
                "doc_date": "2026-02-01",
                "body": "Body A",
            },
            {
                "doc_id": "SYNTH-003",
                "edition": "v1",
                "title": "Synthetic Tax Rule",
                "doc_date": "2026-03-01",
                "body": "Body B",
            },
        ],
    )
    code, out, err = run_cli(["--json", "npa", "search", "environmental"], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 1
    assert data["data"][0]["doc_id"] == "SYNTH-002"


def test_npa_compare_reports_differences(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_npa(
        tmp_path,
        env,
        [
            {
                "doc_id": "SYNTH-004",
                "edition": "v1",
                "title": "Synthetic Rule",
                "doc_date": "2026-01-01",
                "body": "Original body.",
            },
            {
                "doc_id": "SYNTH-004",
                "edition": "v2",
                "title": "Synthetic Rule (Amended)",
                "doc_date": "2026-06-01",
                "body": "Amended body with more text here.",
            },
        ],
    )
    code, out, err = run_cli(
        ["--json", "npa", "compare", "--id", "SYNTH-004", "--edition1", "v1", "--edition2", "v2"],
        env=env,
    )
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["same_body"] is False
    assert data["data"]["body2_length"] > data["data"]["body1_length"]


def test_npa_compare_missing_edition_errors(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_npa(
        tmp_path,
        env,
        [
            {
                "doc_id": "SYNTH-005",
                "edition": "v1",
                "title": "Synthetic Rule",
                "doc_date": "2026-01-01",
                "body": "Body.",
            }
        ],
    )
    code, out, err = run_cli(
        ["--json", "npa", "compare", "--id", "SYNTH-005", "--edition1", "v1", "--edition2", "v2"],
        env=env,
    )
    assert code != 0
    assert "not found" in out.lower() or "not found" in err.lower()


def test_import_npa_duplicate_within_batch_counts_first_occurrence_wins(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    fixture = tmp_path / "npa_dup.json"
    fixture.write_text(
        json.dumps(
            make_npa_fixture(
                [
                    {
                        "doc_id": "DOC-DUP-BATCH",
                        "edition": "v1",
                        "title": "First Title",
                        "doc_date": "2026-01-01",
                        "body": "First body.",
                    },
                    {
                        "doc_id": "DOC-DUP-BATCH",
                        "edition": "v1",
                        "title": "Second Title",
                        "doc_date": "2026-12-31",
                        "body": "Second body.",
                    },
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "npa", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["inserted"] == 1
    assert data["data"]["skipped"] == 1
    assert data["data"]["total"] == 2

    conn = get_connection(tmp_path)
    rows = conn.execute(
        "SELECT * FROM npa_docs WHERE doc_id = ? AND edition = ?",
        ("DOC-DUP-BATCH", "v1"),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "First Title"
    assert rows[0]["body"] == "First body."


def test_import_npa_duplicate_with_different_content_is_skipped_not_overwritten(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    import_npa(
        tmp_path,
        env,
        [
            {
                "doc_id": "DOC-DUP",
                "edition": "v1",
                "title": "Original Title",
                "doc_date": "2026-01-01",
                "body": "Original body.",
            }
        ],
    )

    fixture = tmp_path / "npa_changed.json"
    fixture.write_text(
        json.dumps(
            make_npa_fixture(
                [
                    {
                        "doc_id": "DOC-DUP",
                        "edition": "v1",
                        "title": "Changed Title",
                        "doc_date": "2026-12-31",
                        "body": "Changed body.",
                    }
                ]
            )
        )
    )
    code, out, err = run_cli(["--json", "import", "npa", str(fixture)], env=env)
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["data"]["inserted"] == 0
    assert data["data"]["skipped"] == 1
    assert data["data"]["total"] == 1

    conn = get_connection(tmp_path)
    row = conn.execute(
        "SELECT * FROM npa_docs WHERE doc_id = ? AND edition = ?", ("DOC-DUP", "v1")
    ).fetchone()
    assert row["title"] == "Original Title"
    assert row["body"] == "Original body."
