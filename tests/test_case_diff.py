"""Case version diff over two saved snapshots.

case-search computes the same comparison in ``case_search/diff.py``
(``diff_snapshots``) but never exposes it — no CLI subcommand calls it and
``public_api.py`` does not re-export it. sud-cli therefore implements the same
field set locally, and these tests pin the semantics that matter: identity is
checked before comparison, "nothing changed" is a valid answer, and a missing
or malformed file is a typed error rather than a traceback.
"""

import json

import pytest

from sud_cli.case_diff import (
    SCALAR_FIELDS,
    DiffError,
    diff_case_snapshots,
)


def _snapshot(**overrides):
    base = {
        "case_number": "2а-2939/2025",
        "court": "Московский областной суд",
        "judge": "Белая Светлана Львовна",
        "status": "В производстве",
        "parties": {"plaintiff": ["Иванов"], "respondent": ["Петров"], "third_parties": []},
        "next_hearing": None,
        "last_event": {"date": "01.10.2026", "event_type": "исковое заявление",
                       "document_url": "https://sudact.ru/x/"},
        "events_timeline": [{"date": "01.10.2026", "name": "Исковое заявление",
                             "published": True}],
        "updated_at": "2026-10-09T12:00:00Z",
        "provenance": {"source": "sudrf"},
    }
    base.update(overrides)
    return base


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_identical_snapshots_report_no_change(tmp_path):
    a = _write(tmp_path, "a.json", _snapshot())
    b = _write(tmp_path, "b.json", _snapshot())
    out = diff_case_snapshots(a, b)
    assert out["changed"] is False
    assert out["changes"] == []
    assert out["timeline_changes"] == []


def test_changed_field_is_reported_with_before_and_after(tmp_path):
    a = _write(tmp_path, "a.json", _snapshot())
    b = _write(tmp_path, "b.json", _snapshot(status="УДОВЛЕТВОРИТ"))
    out = diff_case_snapshots(a, b)
    assert out["changed"] is True
    change = next(c for c in out["changes"] if c["path"] == "status")
    assert change["before"] == "В производстве"
    assert change["after"] == "УДОВЛЕТВОРИТ"


def test_null_to_value_is_a_change_not_a_phantom(tmp_path):
    """A first hearing appearing is exactly what an agent looks for."""
    a = _write(tmp_path, "a.json", _snapshot())
    b = _write(
        tmp_path, "b.json",
        _snapshot(next_hearing={"date": "15.11.2026", "time": "10:00",
                                "hall": "312", "type": "заседание"}),
    )
    out = diff_case_snapshots(a, b)
    paths = {c["path"] for c in out["changes"]}
    assert "next_hearing.date" in paths
    assert "next_hearing.hall" in paths
    change = next(c for c in out["changes"] if c["path"] == "next_hearing.date")
    assert change["before"] is None
    assert change["after"] == "15.11.2026"


def test_party_change_is_tracked_per_role(tmp_path):
    a = _write(tmp_path, "a.json", _snapshot())
    b = _write(
        tmp_path, "b.json",
        _snapshot(parties={"plaintiff": ["Иванов", "Сидоров"],
                           "respondent": ["Петров"], "third_parties": []}),
    )
    out = diff_case_snapshots(a, b)
    change = next(c for c in out["changes"] if c["path"] == "parties.plaintiff")
    assert change["before"] == ["Иванов"]
    assert change["after"] == ["Иванов", "Сидоров"]


def test_new_timeline_event_is_reported(tmp_path):
    a = _write(tmp_path, "a.json", _snapshot())
    data = _snapshot()
    data["events_timeline"] = data["events_timeline"] + [
        {"date": "20.10.2026", "name": "Определение суда", "published": True}
    ]
    b = _write(tmp_path, "b.json", data)
    out = diff_case_snapshots(a, b)
    assert out["changed"] is True
    assert len(out["timeline_changes"]) == 1
    entry = out["timeline_changes"][0]
    assert entry["date"] == "20.10.2026"
    assert entry["before"] is None and entry["after"] is True


def test_different_cases_are_refused_before_comparing(tmp_path):
    """A field-by-field report across two cases would be noise."""
    a = _write(tmp_path, "a.json", _snapshot())
    b = _write(tmp_path, "b.json", _snapshot(case_number="А40-99999/2025"))
    with pytest.raises(DiffError, match="different cases"):
        diff_case_snapshots(a, b)


def test_missing_file_is_a_typed_error(tmp_path):
    a = _write(tmp_path, "a.json", _snapshot())
    with pytest.raises(DiffError, match="cannot read snapshot"):
        diff_case_snapshots(a, tmp_path / "нет.json")


def test_malformed_json_is_a_typed_error(tmp_path):
    a = _write(tmp_path, "a.json", _snapshot())
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DiffError, match="not valid JSON"):
        diff_case_snapshots(a, bad)


def test_snapshot_without_case_number_is_refused(tmp_path):
    a = _write(tmp_path, "a.json", {"court": "x"})
    b = _write(tmp_path, "b.json", {"court": "y"})
    with pytest.raises(DiffError, match="case_number"):
        diff_case_snapshots(a, b)


def test_auto_import_envelope_is_unwrapped(tmp_path):
    """auto-import wraps the snapshot; sudrf-import writes it at top level."""
    a = _write(tmp_path, "a.json", {"snapshot": _snapshot(), "extensions": {}})
    b = _write(
        tmp_path, "b.json",
        {"snapshot": _snapshot(status="УДОВЛЕТВОРИТ"), "extensions": {}},
    )
    out = diff_case_snapshots(a, b)
    assert out["case_number"] == "2а-2939/2025"
    assert out["changed"] is True


def test_compared_field_set_matches_case_search():
    """The list mirrors case_search.diff._SCALARS; drift would be silent."""
    paths = {p for p, _ in SCALAR_FIELDS}
    assert paths == {
        "status", "court", "judge", "updated_at",
        "next_hearing.date", "next_hearing.time", "next_hearing.hall",
        "next_hearing.type",
        "last_event.date", "last_event.event_type", "last_event.document_url",
    }