"""v2.9.0 §2 — practice status (S-54): verdict only from LOCAL artifacts.

Never asserts «действует/отменён» из воздуха: verdict is possibly_overturned
only when a local snapshot/diff record or an explicit --related fixture entry
carries an overturn/modification marker; otherwise unknown + hint. No network
is reachable from this module (source gate test below).
"""

import json
import re
from pathlib import Path

from sud_cli import practice_status
from tests.conftest import run_cli

CASE = "А40-99/2026"


def E(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def J(out):
    return json.loads(out)


def write_snapshot(store, name, payload):
    store.mkdir(parents=True, exist_ok=True)
    p = store / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def snapshot(case_number, event_names):
    return {
        "case_number": case_number,
        "status": "Рассматривается в первой инстанции",
        "events_timeline": [{"date": f"2026-01-{i+1:02d}", "name": n, "published": True}
                            for i, n in enumerate(event_names)],
    }


# ------------------------------------------------------------------ verdicts


def test_unknown_for_empty_cache(tmp_path):
    store = tmp_path / "store"  # deliberately not created
    code, out, err = run_cli(
        ["--json", "practice", "status", CASE, "--store", str(store)], env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "unknown"
    assert data["evidence"] == []
    assert data["hint"]


def test_overturn_found_in_local_snapshot(tmp_path):
    store = tmp_path / "store"
    snap = snapshot(CASE, ["Решение отменено апелляционной инстанцией, принято новое решение"])
    path = write_snapshot(store, f"{CASE.replace('/', '-')}.json", snap)
    code, out, err = run_cli(
        ["--json", "practice", "status", CASE, "--store", str(store)], env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "possibly_overturned"
    ev = data["evidence"][0]
    assert str(path) == ev["file"]
    assert "отменено" in ev["title"]


def test_upheld_without_change_is_not_overturn(tmp_path):
    store = tmp_path / "store"
    write_snapshot(store, "s.json", snapshot(CASE, ["Оставлено без изменения"]))
    code, out, err = run_cli(
        ["--json", "practice", "status", CASE, "--store", str(store)], env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "unknown"


def test_diff_artifact_status_change_detected(tmp_path):
    store = tmp_path / "store"
    write_snapshot(store, "diff-A40-99-2026.json", {
        "case_number": CASE, "changed": True,
        "changes": [{"path": "status",
                     "before": "Рассматривается в первой инстанции",
                     "after": "Решение отменено, дело прекращено"}],
        "timeline_changes": [],
    })
    code, out, err = run_cli(
        ["--json", "practice", "status", CASE, "--store", str(store)], env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "possibly_overturned"
    assert any("отмен" in e["title"] for e in data["evidence"])


def test_related_fixture_mode_no_network(tmp_path):
    code, out, err = run_cli(
        ["--json", "practice", "status", CASE,
         "--related", f"А40-100/2026 постановление отменено;А40-101/2026 решение изменено"],
        env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "possibly_overturned"
    assert len(data["evidence"]) == 2
    assert all(e["file"].startswith("cli:--related") for e in data["evidence"])
    assert data["hint"]


def test_hint_always_present_for_both_verdicts(tmp_path):
    store = tmp_path / "store"
    write_snapshot(store, "s.json", snapshot(CASE, ["Решение отменено"]))
    code, out, err = run_cli(["--json", "practice", "status", CASE, "--store", str(store)], env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["hint"]
    assert not (store / "nothing").exists()
    code, out, err = run_cli(
        ["--json", "practice", "status", CASE, "--store", str(tmp_path / "missing")], env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["hint"]


def test_never_claims_deystvuet(tmp_path):
    store = tmp_path / "store"
    write_snapshot(store, "s.json", snapshot(CASE, ["Решение оставлено без отмены"]))
    for args in (["--store", str(store)], ["--store", str(tmp_path / "none")]):
        code, out, err = run_cli(["--json", "practice", "status", CASE, *args], env=E(tmp_path))
        assert code == 0, err
        assert "действует" not in out
    # And a pure-function call never yields a lifecycle assertion either.
    payload = practice_status.assess_status(CASE, store_dir=store)
    assert payload["verdict"] in {"unknown", "possibly_overturned"}
    assert "действует" not in json.dumps(payload, ensure_ascii=False)


def test_empty_case_number_invalid(tmp_path):
    code, out, err = run_cli(["--json", "practice", "status", "", "--store", str(tmp_path)], env=E(tmp_path))
    assert code != 0
    assert J(err)["data"]["status"] == "INVALID_ARGUMENTS"


def test_command_forms_practice_status_and_flat_alias(tmp_path):
    env = E(tmp_path)
    for argv in (["--json", "practice", "status", CASE],
                 ["--json", "practice-status", CASE]):
        code, out, err = run_cli(argv, env=env)
        assert code == 0, err
        assert J(out)["data"]["verdict"] == "unknown"


# ------------------------------------------------------------------ gates


def test_practice_status_source_has_no_network_primitives():
    src = Path(practice_status.__file__).read_text(encoding="utf-8")
    forbidden = re.compile(r"\b(requests|urllib|socket|httpx|subprocess)\b|https?://")
    assert not forbidden.search(src), forbidden.search(src).group(0)


def test_pure_function_signature_accepts_related_list():
    payload = practice_status.assess_status(CASE, related=["что-то отменено"])
    assert payload["verdict"] == "possibly_overturned"
    assert payload["case_number"] == CASE
