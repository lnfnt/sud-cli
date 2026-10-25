"""история §5: S-85 backup/verify/restore, S-86 metrics, S-07 export/purge + MCP.

Отдельные границы, проверяемые здесь:
- restore и purge — деструктив: без --yes/confirm ничего не меняется;
- снимок и sidecar — 0600, sha256 воспроизводится, restore идёт в tmp-state
  (живой state-dir в тестах не трогается);
- purge НЕ пишет ПДн в audit: grep фикстурного имени по журналу = 0;
- pdn_policy в export/purge — дословно из legal_rules.json, а не строка
  «на память»; retention-строка живёт в правилах;
- MCP: новые тулы зарегистрированы, permission по матрице, счётчик тулов
  двигается волной.
"""
import json
import os
import stat
from datetime import date, timedelta

import pytest
from sud_cli import mcp_server
from sud_cli.backup import create_backup, restore_backup, verify_backup
from sud_cli.db import (
    add_case_amount,
    add_case_document,
    add_case_evidence,
    add_case_note,
    add_case_representative,
    anchor_deadline_state,
    create_case,
    get_case,
    get_connection,
    init_db,
    insert_deadline_state,
    list_all_deadline_states,
    read_deadline_audit,
)
from sud_cli.legal_engine import load_rules
from sud_cli.metrics import metrics_payload
from sud_cli.pdn import export_case, purge_apply, purge_preview, render_export_md
from tests.conftest import run_cli

FIXTURE_PERSON = "Иванова Мария Петровна"
FIXTURE_SECRET = "тайна переписки из доверенности"


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _full_case(state_dir):
    """Дело со строками во всех таблицах + физический файл + след в audit."""
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        cid = create_case(conn, "A117-1/2026", "АС г. Москвы", FIXTURE_PERSON,
                          "ИП Бета", status="pending", instance="apk")["case_id"]
        add_case_amount(conn, cid, "fee", "1000")
        add_case_note(conn, cid, f"звонок {FIXTURE_PERSON}", author="lex")
        add_case_representative(conn, cid, side="plaintiff", name=FIXTURE_PERSON,
                                power_docs=FIXTURE_SECRET)
        doc = state_dir / "dokument.txt"
        doc.write_text(f"содержание для {FIXTURE_PERSON}", encoding="utf-8")
        add_case_document(conn, cid, str(doc))
        add_case_evidence(conn, cid, title="Доверенность", path=str(doc))
        # method/evidence намеренно содержат ПДн: их записывает якорь, и именно
        # их purge обязан вычистить из журнала (grep-гейт ниже это проверяет).
        anchor_deadline_state(conn, state_dir, cid, "as_appeal_decision",
                              date.today().isoformat(),
                              f"вручение {FIXTURE_PERSON} под роспись",
                              FIXTURE_SECRET, actor="lex")
        insert_deadline_state(conn, cid, "as_cassation_district",
                              due_at=(date.today() + timedelta(days=20)).isoformat(),
                              state="computed")
    finally:
        conn.close()
    return cid, doc


# ---------------------------------------------------------------- S-85 backup
def test_backup_create_then_verify_green(tmp_path):
    _full_case(tmp_path)
    info = create_backup(tmp_path)
    assert os.path.isfile(info["path"])
    assert info["tables"]["cases"] == 1
    report = verify_backup(info["path"])
    assert report["integrity_ok"] is True
    assert report["quick_ok"] is True
    assert report["schema_version"] == "3"
    assert "case_representatives" in report["tables"]
    assert report["sidecar_match"] is True


def test_backup_file_and_sidecar_are_0600(tmp_path):
    init_db(tmp_path)
    info = create_backup(tmp_path)
    for path in (info["path"], info["sidecar"]):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, (path, oct(mode))


def test_backup_sha_sidecar_matches_recomputed_digest(tmp_path):
    import hashlib

    init_db(tmp_path)
    info = create_backup(tmp_path)
    digest = hashlib.sha256(open(info["path"], "rb").read()).hexdigest()
    assert info["sha256"] == digest
    sidecar_text = open(info["sidecar"], encoding="utf-8").read()
    assert sidecar_text.split()[0] == digest
    assert info["path"] in json.dumps(metrics_payload(tmp_path)["backup"])


def test_backup_explicit_out_path(tmp_path):
    init_db(tmp_path)
    target = tmp_path / "outside" / "snap.db"
    info = create_backup(tmp_path, out=target)
    assert info["path"] == str(target)
    assert target.is_file()
    assert verify_backup(target)["integrity_ok"] is True


def test_backup_verify_missing_file_errors(tmp_path):
    init_db(tmp_path)
    code, out, err = run_cli(["--json", "backup", "verify",
                              str(tmp_path / "net-takogo.db")], env=_env(tmp_path))
    assert code != 0
    assert "FILE_MISSING" in (out + err)


def test_restore_without_yes_changes_nothing(tmp_path):
    cid, _doc = _full_case(tmp_path)
    info = create_backup(tmp_path)
    # состояние изменилось ПОСЛЕ снимка: добавлено второе дело
    conn = get_connection(tmp_path)
    try:
        create_case(conn, "A117-2/2026", "АС СПб", "ООО Gamma", "ИП Delta",
                    status="new")
    finally:
        conn.close()

    result = restore_backup(tmp_path, info["path"], confirmed=False)
    assert result["status"] == "needs_confirmation"
    assert result["preview"]["target_exists"] is True
    assert result["preview"]["target_tables"]["cases"] == 2
    assert result["preview"]["source_tables"]["cases"] == 1
    conn = get_connection(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 2
    finally:
        conn.close()


def test_restore_yes_replaces_state_in_tmp_dir(tmp_path):
    cid, _doc = _full_case(tmp_path)
    info = create_backup(tmp_path)
    conn = get_connection(tmp_path)
    try:
        extra = create_case(conn, "A117-3/2026", "АС Казани", "ООО Epsilon",
                            "ИП Zeta", status="new")["case_id"]
    finally:
        conn.close()
    result = restore_backup(tmp_path, info["path"], confirmed=True)
    assert result["status"] == "restored"
    assert result["tables_after"]["cases"] == 1
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, extra) is None
        assert get_case(conn, cid) is not None
    finally:
        conn.close()


def test_restore_cli_requires_yes_and_reports(tmp_path):
    init_db(tmp_path)
    env = _env(tmp_path)
    info = create_backup(tmp_path)
    code, out, err = run_cli(["--json", "backup", "restore", info["path"]], env=env)
    assert code != 0, "деструктив без --yes обязан отказывать"
    assert "needs_confirmation" in (out + err)
    code, out, err = run_cli(["--json", "backup", "restore", info["path"], "--yes"],
                             env=env)
    assert code == 0, err
    assert _j(out)["data"]["status"] == "restored"


def test_restore_rejects_corrupted_backup(tmp_path):
    init_db(tmp_path)
    info = create_backup(tmp_path)
    with open(info["path"], "r+b") as fh:
        fh.seek(60)
        fh.write(b"\x00" * 512)
    with pytest.raises(Exception):
        restore_backup(tmp_path, info["path"], confirmed=True)


# ---------------------------------------------------------------- S-86 metrics
def test_metrics_counts_come_from_tables(tmp_path):
    cid, _doc = _full_case(tmp_path)
    payload = metrics_payload(tmp_path)
    assert payload["cases"]["total"] == 1
    assert payload["cases"]["by_status"]["pending"] == 1
    assert payload["events"] == payload["rows_per_table"]["events"]
    assert payload["representatives"]["total"] == 1
    assert payload["representatives"]["by_side"] == {"plaintiff": 1}
    assert payload["amounts"] == 1
    assert payload["notes"] == 1
    assert payload["documents"]["total"] == 1
    assert payload["evidence"]["total"] == 1
    ds = payload["deadline_states"]
    assert ds["total"] == len(list_all_deadline_states(get_connection(tmp_path)))
    assert sum(ds["by_state"].values()) == ds["total"]
    assert payload["reminders"]["pending"] >= 0
    assert payload["disclaimer"]


def test_metrics_empty_state_has_zeroed_sections(tmp_path):
    init_db(tmp_path)
    payload = metrics_payload(tmp_path)
    assert payload["cases"]["total"] == 0
    assert payload["deadline_states"]["by_state"] == {}
    assert payload["representatives"]["by_side"] == {}
    assert payload["rows_per_table"]["case_representatives"] == 0
    assert payload["backup"]["last"]["path"] is None
    assert payload["backup"]["last"]["exists"] is False


def test_metrics_no_invented_kpi_keys(tmp_path):
    payload = metrics_payload(tmp_path)
    banned = ("score", "health_index", "success", "rating", "kpi", "efficiency")
    data = {k: v for k, v in payload.items() if k != "disclaimer"}
    text = json.dumps(data, ensure_ascii=False).lower()
    for key in banned:
        assert key not in text, key
    # disclaimer имеет право называть отсутствующее своими словами
    assert "KPI" in payload["disclaimer"]


def test_metrics_cli_json_and_doctor_section(tmp_path):
    _full_case(tmp_path)
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "metrics"], env=env)
    assert code == 0, err
    doc = _j(out)
    assert doc["meta"]["command"] == "metrics"
    assert doc["data"]["cases"]["total"] == 1

    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0, err
    metrics = _j(out)["data"]["metrics"]
    assert metrics["cases"]["total"] == 1
    # additive: прежние ключи doctor не сдвинуты
    for key in ("schema_version", "integrity_ok", "healthy", "reminders_available",
                "alerts_pending", "freshness", "calendar"):
        assert key in _j(out)["data"], key


def test_metrics_human_mode(tmp_path):
    _full_case(tmp_path)
    code, out, err = run_cli(["metrics"], env=_env(tmp_path))
    assert code == 0, err
    assert "Метрики state-dir на" in out
    assert "дела: 1" in out


# ------------------------------------------------------------- S-07 export
def test_export_json_has_all_sections_and_counts(tmp_path):
    cid, _doc = _full_case(tmp_path)
    payload = export_case(tmp_path, cid)
    for section in ("case", "events", "amounts", "documents", "evidence",
                    "representatives", "deadline_states", "notes", "audit"):
        assert section in payload, section
    from sud_cli.pdn import EXPORT_SECTIONS
    assert payload["sections"] == list(EXPORT_SECTIONS)
    assert payload["row_counts"]["cases"] == 1
    assert payload["row_counts"]["case_representatives"] == 1
    # якорь (computed) + вторая точка = 2 строки состояний
    assert payload["row_counts"]["deadline_states"] == 2
    assert payload["representatives"]["items"][0]["power_docs"] == FIXTURE_SECRET


def test_export_pdn_block_is_verbatim_from_rules(tmp_path):
    cid, _doc = _full_case(tmp_path)
    policy = load_rules()["pdn_policy"]
    payload = export_case(tmp_path, cid)
    assert payload["pdn"]["source"] == "legal_rules.json:pdn_policy"
    assert payload["pdn"]["grounds"] == policy["grounds"]
    assert payload["pdn"]["retention"] == policy["retention"]
    assert payload["pdn"]["subject_rights"] == policy["subject_rights"]
    assert payload["pdn"]["legal_status"] == policy["legal_status"]


def test_export_markdown_renders_sections(tmp_path):
    cid, _doc = _full_case(tmp_path)
    text = render_export_md(export_case(tmp_path, cid, fmt="md"))
    assert f"# Экспорт дела {cid}" in text
    assert "## Представители" in text
    assert "## ПДн: основания и срок" in text
    policy = load_rules()["pdn_policy"]
    assert policy["retention"] in text
    assert "## События\n\n_пусто_" in text


def test_export_cli_json_and_out_file(tmp_path):
    cid, _doc = _full_case(tmp_path)
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "export", cid], env=env)
    assert code == 0, err
    assert _j(out)["data"]["case_id"] == cid

    target = tmp_path / "dump.md"
    code, out, err = run_cli(["--json", "case", "export", cid, "--format", "md",
                              "--out", str(target)], env=env)
    assert code == 0, err
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert body.startswith(f"# Экспорт дела {cid}")
    assert "A117-1/2026" in body


def test_export_unknown_case_errors(tmp_path):
    code, out, err = run_cli(["--json", "case", "export", "case-absent"],
                             env=_env(tmp_path))
    assert code != 0
    assert "case not found" in (out + err)


# -------------------------------------------------------------- S-07 purge
def test_purge_without_yes_refuses_and_previews(tmp_path):
    cid, doc = _full_case(tmp_path)
    preview = purge_preview(tmp_path, cid)
    assert preview["will_delete_rows"]["cases"] == 1
    assert preview["will_delete_rows"]["case_representatives"] == 1
    assert preview["will_delete_files"] == [str(doc)]
    assert preview["retention_hint"] == load_rules()["pdn_policy"]["retention"]

    result = purge_apply(tmp_path, cid, confirmed=False)
    assert result["status"] == "needs_confirmation"
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, cid) is not None, "без подтверждения дело обязано остаться"
    finally:
        conn.close()


def test_purge_cli_requires_yes(tmp_path):
    cid, _doc = _full_case(tmp_path)
    env = _env(tmp_path)
    code, out, err = run_cli(["--json", "case", "purge", cid], env=env)
    assert code != 0
    data = _j(err)["data"]
    assert data["status"] == "needs_confirmation"
    assert data["preview"]["will_delete_rows"]["cases"] == 1


def test_purge_removes_rows_of_every_table_and_files(tmp_path):
    cid, doc = _full_case(tmp_path)
    result = purge_apply(tmp_path, cid, confirmed=True)
    assert result["status"] == "purged"
    assert result["removed_rows"]["cases"] == 1
    for table, count in result["removed_rows"].items():
        conn = get_connection(tmp_path)
        try:
            left = conn.execute(f"SELECT COUNT(*) AS n FROM {table}"
                                " WHERE case_id = ?", (cid,)).fetchone()["n"]
        finally:
            conn.close()
        assert left == 0, (table, count)
    assert doc.exists() is False
    assert result["files"]["deleted"] == 1
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, cid) is None
    finally:
        conn.close()


def test_purge_audit_contains_no_pii(tmp_path):
    """grep фикстурного имени/секрета по журналу = 0: audit несёт только факт."""
    cid, doc = _full_case(tmp_path)
    audit_file = tmp_path / "deadline_audit.jsonl"
    assert FIXTURE_PERSON in audit_file.read_text(encoding="utf-8"), \
        "до purge в журнале есть ПДн — иначе гейт ничего не проверяет"
    purge_apply(tmp_path, cid, confirmed=True)

    text = audit_file.read_text(encoding="utf-8")
    assert FIXTURE_PERSON not in text
    assert FIXTURE_SECRET not in text
    assert str(doc) not in text
    entries = read_deadline_audit(tmp_path)
    purge_entry = [e for e in entries if e.get("action") == "purge_case"]
    assert len(purge_entry) == 1
    assert purge_entry[0]["case"] == cid
    assert set(purge_entry[0]) == {"ts", "actor", "case", "action", "removed_rows",
                                   "files_deleted", "files_missing", "files_failed",
                                   "files_outside_statedir", "audit_lines_scrubbed"}
    assert purge_entry[0]["audit_lines_scrubbed"] >= 1


def test_purge_keeps_other_cases_and_their_audit(tmp_path):
    cid, _doc = _full_case(tmp_path)
    conn = get_connection(tmp_path)
    try:
        other = create_case(conn, "A117-9/2026", "АС СПб", "ООО Gamma", "ИП Delta",
                            status="new")["case_id"]
        anchor_deadline_state(conn, tmp_path, other, "as_appeal_decision",
                              date.today().isoformat(), "вручение", "ostorozhno",
                              actor="lex")
    finally:
        conn.close()
    purge_apply(tmp_path, cid, confirmed=True)
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, other) is not None
    finally:
        conn.close()
    kept = read_deadline_audit(tmp_path, other)
    assert any(e.get("action") == "anchor" for e in kept)


def test_purge_keep_files_option(tmp_path):
    cid, doc = _full_case(tmp_path)
    result = purge_apply(tmp_path, cid, confirmed=True, remove_files=False)
    assert result["files_kept"] is True
    assert doc.exists() is True
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, cid) is None
    finally:
        conn.close()


# ------------------------------------------------------------------------- MCP
def _call(name, arguments, request_id=7):
    return mcp_server.handle_request({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


W6B2_TOOLS = {
    "sud_cli_deadlines_chain": "read",
    "sud_cli_case_rep_add": "write",
    "sud_cli_case_rep_list": "read",
    "sud_cli_calendar_conflicts": "read",
    "sud_cli_report_portfolio": "read",
    "sud_cli_report_case": "read",
    "sud_cli_metrics": "read",
    "sud_cli_backup_create": "write",
    "sud_cli_backup_verify": "read",
    "sud_cli_backup_restore": "write",
    "sud_cli_case_export": "read",
    "sud_cli_case_purge": "write",
}


def test_mcp_wave_tools_registered_with_expected_permission():
    names = {t["name"] for t in mcp_server.tool_list()["tools"]}
    assert set(W6B2_TOOLS) <= names
    for name, permission in W6B2_TOOLS.items():
        assert mcp_server.tool_permission(name) == permission, name
    # счётчик тулов двигается волной: 12 новых тулов зарегистрированы
    # v2.14.0 S-03: +1 egrul; v2.15.0 S-61: +3 fssp поверх волны the wave
    # v0.1.0 §13-интерфейс: +1 license (§13)
    # v0.1.0 W7: +2 (efrsb_link, plenum_search)
    assert len(mcp_server.TOOLS) == 46 + len(W6B2_TOOLS) + 7


def test_mcp_write_wave_tools_need_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    init_db(tmp_path)
    for name, args in (
        ("sud_cli_backup_create", {}),
        ("sud_cli_backup_restore", {"file": "/tmp/nope.db"}),
        ("sud_cli_case_purge", {"case_id": "case-x"}),
        ("sud_cli_case_rep_add", {"case_id": "case-x", "side": "plaintiff",
                                  "name": "X"}),
    ):
        body = _body(_call(name, args))
        assert body["status"] == "needs_confirmation", (name, body)
        assert body["permission"] == "write", name


def test_mcp_case_rep_add_and_list_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    cid = _body(_call("sud_cli_case_new", {
        "number": "A117-20/2026", "court": "АС г. Москвы", "plaintiff": "P",
        "defendant": "D", "confirm": True}))["case_id"]
    body = _body(_call("sud_cli_case_rep_add", {
        "case_id": cid, "side": "defendant", "name": "Петров П.П.",
        "power_docs": "дов. №9", "confirm": True}))
    assert body["side"] == "defendant"
    assert "вручн" in body["hint"]
    listed = _body(_call("sud_cli_case_rep_list", {"case_id": cid}))
    assert [i["name"] for i in listed["items"]] == ["Петров П.П."]


def test_mcp_chain_and_conflicts_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    cid = _body(_call("sud_cli_case_new", {
        "number": "A117-21/2026", "court": "АС г. Москвы", "plaintiff": "P",
        "defendant": "D", "confirm": True}))["case_id"]
    chain = _body(_call("sud_cli_deadlines_chain", {"case_id": cid,
                                                    "instance": "apk"}))
    assert chain["instance"] == "apk"
    assert [p["key"] for p in chain["chain"]] == ["as_appeal_decision",
                                                  "as_cassation_district"]
    assert all(p["due_at"] is None for p in chain["chain"])
    assert "справ" in chain["disclaimer"].lower()

    conflicts = _body(_call("sud_cli_calendar_conflicts", {"days": 30}))
    assert conflicts["conflicts"] == []
    portfolio = _body(_call("sud_cli_report_portfolio", {}))
    assert portfolio["cases"]["total"] == 1
    case_report = _body(_call("sud_cli_report_case", {"case_id": cid}))
    assert case_report["case_id"] == cid
    metrics = _body(_call("sud_cli_metrics", {"near_days": 45}))
    assert metrics["deadlines_window"]["near_days"] == 45


def test_mcp_backup_create_verify_restore_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    cid = _body(_call("sud_cli_case_new", {
        "number": "A117-22/2026", "court": "АС г. Москвы", "plaintiff": "P",
        "defendant": "D", "confirm": True}))["case_id"]
    info = _body(_call("sud_cli_backup_create", {"confirm": True}))
    assert info["tables"]["cases"] == 1
    check = _body(_call("sud_cli_backup_verify", {"file": info["path"]}))
    assert check["ok"] is True
    assert check["sidecar_match"] is True

    _body(_call("sud_cli_case_new", {
        "number": "A117-23/2026", "court": "АС Казани", "plaintiff": "P2",
        "defendant": "D2", "confirm": True}))
    restored = _body(_call("sud_cli_backup_restore", {"file": info["path"],
                                                     "confirm": True}))
    assert restored["status"] == "restored"
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, cid) is not None
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
    finally:
        conn.close()


def test_mcp_case_export_includes_pdn_and_purge_destroys(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    cid, doc = _full_case(tmp_path)
    policy = load_rules()["pdn_policy"]
    exported = _body(_call("sud_cli_case_export", {"case_id": cid}))
    assert exported["pdn"]["grounds"] == policy["grounds"]
    assert exported["row_counts"]["case_representatives"] == 1
    assert exported["representatives"]["items"][0]["power_docs"] == FIXTURE_SECRET

    purged = _body(_call("sud_cli_case_purge", {"case_id": cid, "confirm": True}))
    assert purged["status"] == "purged"
    assert doc.exists() is False
    text = (tmp_path / "deadline_audit.jsonl").read_text(encoding="utf-8")
    assert FIXTURE_PERSON not in text
    assert FIXTURE_SECRET not in text


def test_mcp_tools_have_cli_parity_paths():
    for name in W6B2_TOOLS:
        assert name in mcp_server.TOOL_CLI_PATH, name
    exposed = mcp_server.mcp_exposed_commands()
    for name, path in mcp_server.TOOL_CLI_PATH.items():
        if name in W6B2_TOOLS:
            assert path in exposed


def test_mcp_unknown_field_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    resp = _call("sud_cli_report_portfolio", {"days": 10, "typo": 1})
    assert resp["result"]["isError"] is True
    assert "unknown fields" in resp["result"]["content"][0]["text"]
