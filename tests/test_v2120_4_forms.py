"""the wave §4 (S-70): скелеты процессуальных форм + bus-free docx-команда + MCP-тул.

Границы: вывод — только структура с плейсхолдерами; нормативных цитат нет ни в
forms.py, ни в выводе (гейт regex «ст.\\d» и «ст. \\d»). deferral НЕ дублируется
(cmd_deferral остаётся как есть — регресс-тест). sud никогда не запускает docx:
в forms.py нет subprocess/os.system, строка только генерируется (паттерн reminders).
"""
import json
import re
from pathlib import Path

from sud_cli import mcp_server
from tests.conftest import run_cli

#: обе формы написания: «ст.395» и «ст. 395» — нормативная цитата запрещена
ART_RE = re.compile(r"ст\.\s*\d")
FORM_KINDS = ("vosstanovlenie_sroka", "otsrochka", "appeal_structure")


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk_case(tmp_path, number="A150-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "АС г. Москвы",
         "--plaintiff", "ООО Ромашка", "--defendant", "ИП Крокодил"], env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _form(env, kind, case_id, fmt="md"):
    return run_cli(["--json", "form", "new", kind, "--case", case_id,
                    "--format", fmt], env=env)


def test_form_new_each_kind_renders_skeleton_with_case_data(tmp_path):
    env, case_id = _mk_case(tmp_path, "A150-2/2026")
    for kind in FORM_KINDS:
        code, out, err = _form(env, kind, case_id)
        assert code == 0, err
        data = _j(out)["data"]
        assert data["kind"] == kind
        assert "A150-2/2026" in data["text"]
        assert "АС г. Москвы" in data["text"]
        assert "[ФИО]" in data["text"] or "ФИО" in data["text"]
        assert "## Просьба" in data["text"]


def test_form_skeletons_have_structural_markers_of_checklist(tmp_path):
    """SYNC §3<->§4: полный скелет каждой формы проходит свой checklist-kind."""
    from sud_cli.completeness import evaluate, load_rules
    from sud_cli.forms import CHECKLIST_KIND_FOR_FORM, render_form
    rules = load_rules()
    case = {"case_number": "A150-9/2026", "court": "АС", "plaintiff": "П", "defendant": "Д"}
    for kind in FORM_KINDS:
        payload = render_form(kind, case)
        res = evaluate(payload["text"], CHECKLIST_KIND_FOR_FORM[kind], rules)
        assert res["missing"] == [], (kind, res)


def test_form_output_has_no_stat_citations(tmp_path):
    env, case_id = _mk_case(tmp_path, "A150-3/2026")
    for kind in FORM_KINDS:
        for fmt in ("md", "json"):
            code, out, err = _form(env, kind, case_id, fmt=fmt)
            assert code == 0, err
            assert not ART_RE.search(out + err), kind


def test_forms_source_has_no_stat_citations():
    src = (Path(__file__).resolve().parents[1] / "sud_cli" / "forms.py").read_text(encoding="utf-8")
    assert not ART_RE.search(src)


def test_form_new_json_format_sections(tmp_path):
    env, case_id = _mk_case(tmp_path, "A150-4/2026")
    code, out, err = _form(env, "appeal_structure", case_id, fmt="json")
    assert code == 0, err
    data = _j(out)["data"]
    assert data["format"] == "json"
    headings = [s["heading"] for s in data["sections"]]
    assert "Обжалуется" in headings and "Основания" in headings


def test_form_new_case_not_found_errors(tmp_path):
    env, _ = _mk_case(tmp_path, "A150-5/2026")
    code, out, err = _form(env, "otsrochka", "no-such-case")
    assert code != 0


def test_deferral_not_duplicated_and_not_broken(tmp_path):
    """Регресс: cmd_deferral живёт своей командой; forms.py текст не копирует."""
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "deferral", "--applicant", "ИП П", "--court", "АС",
         "--amount", "6000", "--reason", "задержка зарплаты"], env=env)
    assert code == 0, err
    assert "отсрочке уплаты государственной пошлины" in _j(out)["data"]["text"]
    from sud_cli.deferral import generate_deferral
    direct = generate_deferral("ИП П", "АС", 6000, "задержка зарплаты")
    assert _j(out)["data"]["text"] == direct["text"]
    forms_src = (Path(__file__).resolve().parents[1] / "sud_cli" / "forms.py").read_text(encoding="utf-8")
    assert "Постановления Пленума ВАС" not in forms_src  # скелет не копирует deferral-текст
    cli_src = (Path(__file__).resolve().parents[1] / "sud_cli" / "cli.py").read_text(encoding="utf-8")
    assert "def cmd_deferral" in cli_src  # команда осталась на месте


def test_docx_command_shlex_parses_and_ends_with_verify_run(tmp_path):
    import shlex
    env, case_id = _mk_case(tmp_path, "A150-6/2026")
    code, out, err = run_cli(
        ["--json", "form", "docx-command", "vosstanovlenie_sroka", "--case", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    parts = shlex.split(data["command"])
    assert parts[0] == "docx-cli" and parts[1] == "build"
    i = parts.index("--spec")
    assert Path(parts[i + 1]).is_file()
    assert parts[-1] == "--verify-run"
    assert parts[-2] == f"/usr/bin/sud-cli form new vosstanovlenie_sroka --case {case_id} --format md"
    assert parts[-3] == "--verify"
    spec = json.loads(Path(parts[i + 1]).read_text(encoding="utf-8"))
    assert spec["kind"] == "vosstanovlenie_sroka"


def test_docx_command_source_is_bus_free():
    """sud никогда не вызывает docx: в forms.py нет средств запуска процессов."""
    src = (Path(__file__).resolve().parents[1] / "sud_cli" / "forms.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "Popen", "socket", "requests"):
        assert forbidden not in src


def test_docx_command_quotes_special_paths(tmp_path):
    import shlex
    env, case_id = _mk_case(tmp_path, "A150-7/2026")
    weird = tmp_path / "spec dir with 'quotes' and space"
    code, out, err = run_cli(
        ["--json", "form", "docx-command", "otsrochka", "--case", case_id,
         "--spec-dir", str(weird)], env=env)
    assert code == 0, err
    parts = shlex.split(_j(out)["data"]["command"])
    i = parts.index("--spec")
    assert parts[i + 1] == str(weird / f"otsrochka-{case_id}-spec.json")


def test_mcp_form_new_registered_read_permission():
    names = {t["name"]: t for t in mcp_server.tool_list()["tools"]}
    assert "sud_cli_form_new" in names
    tool = names["sud_cli_form_new"]
    assert tool["permission"] == "read"
    assert tool["inputSchema"]["additionalProperties"] is False
    assert set(tool["inputSchema"]["properties"]) == {"kind", "case_id", "format"}
    assert mcp_server.TOOL_CLI_PATH["sud_cli_form_new"] == ("form", "new")


def test_mcp_form_new_renders_and_gates_unknown_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_CLI_STATE_DIR", str(tmp_path))
    env = _env(tmp_path)
    _, case_id = _mk_case(tmp_path, "A150-8/2026")
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sud_cli_form_new",
                   "arguments": {"kind": "otsrochka", "case_id": case_id}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["status"] == "ok" and "A150-8/2026" in body["text"]
    resp2 = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "sud_cli_form_new",
                   "arguments": {"kind": "otsrochka", "case_id": case_id, "foo": 1}}})
    assert resp2["result"]["isError"] is True
