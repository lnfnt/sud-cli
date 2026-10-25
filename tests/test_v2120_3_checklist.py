"""the wave §3 (S-71): структурный чек-лист комплектности — НЕ юридическая проверка.

completeness_rules.json — маркеры формы шаблона (шапка/[суд]/[истец]/[ответчик],
Просьба, Приложения, Подпись, Дата; appeals: +Обжалуется, +Основания).
Ни одной нормативной ссылки «ст. N» ни в файле правил, ни в выводе.
"""
import json
import re

from tests.conftest import run_cli

ARTICLE_RE = re.compile(r"ст\.\s*\d")


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk_case(tmp_path, number="A140-1/2026"):
    env = _env(tmp_path)
    code, out, err = run_cli(
        ["--json", "case", "new", "--number", number, "--court", "АС", "--plaintiff", "П",
         "--defendant", "Д"], env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _skeleton(tmp_path, name="hodat.txt", form_kind="vosstanovlenie_sroka"):
    """Скелет формы из §4 — единый набор маркеров с completeness-правилами."""
    from sud_cli.forms import render_form
    payload = render_form(form_kind,
                          {"case_number": "А140-9/2026", "court": "АС г. Москвы",
                           "plaintiff": "ООО Ромашка", "defendant": "ИП Крокодил"})
    path = tmp_path / name
    path.write_text(payload["text"], encoding="utf-8")
    return path


def _doc_add(env, case_id, path):
    code, out, err = run_cli(["--json", "case", "doc", "add", case_id, "--path", str(path)], env=env)
    assert code == 0, err
    return _j(out)["data"]["id"]


def test_rules_file_valid_json_and_no_stat_refs():
    from pathlib import Path
    import sud_cli
    rules_path = Path(sud_cli.__file__).resolve().parent / "data" / "completeness_rules.json"
    raw = rules_path.read_text(encoding="utf-8")
    data = json.loads(raw)  # валидный JSON
    assert not ARTICLE_RE.search(raw), "в rules-файле не может быть нормативных ссылок"
    assert data["meta"]["format"] == "format-checklist"
    assert "не юридическ" in data["meta"]["disclaimer"].lower()
    for kind in ("isk_apk", "claim_apk", "appeal_apk", "hodat"):
        assert kind in data["kinds"]
    for m in ("Просьба", "Приложения", "Подпись", "Дата"):
        assert m in data["kinds"]["isk_apk"]
    for m in ("Обжалуется", "Основания"):
        assert m in data["kinds"]["appeal_apk"]


def test_checklist_full_skeleton_all_found(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-2/2026")
    doc_id = _doc_add(env, case_id, _skeleton(tmp_path))
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "hodat"], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert set(data["checklist"].values()) == {"found"}
    assert data["verdict"] == "структурно полный"
    assert data["missing"] == []


def test_checklist_missing_prosoba(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-3/2026")
    p = _skeleton(tmp_path, name="bez-prosby.txt")
    text = p.read_text(encoding="utf-8").replace("## Просьба", "## Другой раздел")
    p.write_text(text, encoding="utf-8")
    doc_id = _doc_add(env, case_id, p)
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "hodat"], env=env)
    assert code == 0, err  # это справка, не валяция
    data = _j(out)["data"]
    assert data["checklist"]["Просьба"] == "missing"
    assert data["verdict"] == "структурно неполный"


def test_checklist_no_stat_in_output(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-4/2026")
    doc_id = _doc_add(env, case_id, _skeleton(tmp_path))
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "hodat"], env=env)
    assert not ARTICLE_RE.search(out + err)


def test_checklist_binary_doc_need_text_hint(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-5/2026")
    p = tmp_path / "scan.docx"
    p.write_bytes(b"PK\x03\x04\xfe\xff\x80\x81not-utf8-binary")
    doc_id = _doc_add(env, case_id, p)
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "isk_apk"], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    assert data["need_text"] is True
    assert "need-text" in json.dumps(data, ensure_ascii=False)


def test_checklist_unknown_doc_empty(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-6/2026")
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, "42", "--kind", "hodat"], env=env)
    assert code == 0, err  # пустой case: пустой чек-лист, не провал
    data = _j(out)["data"]
    assert data["checklist"] == {}
    assert data["found"] is False


def test_checklist_disclaimer_in_output(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-7/2026")
    doc_id = _doc_add(env, case_id, _skeleton(tmp_path))
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "hodat"], env=env)
    blob = json.dumps(_j(out), ensure_ascii=False)
    assert "не юридическ" in blob.lower()


def test_checklist_appeal_requires_obzhualuetsya(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-8/2026")
    from sud_cli.forms import render_form
    payload = render_form("appeal_structure",
                          {"case_number": "А140-9/2026", "court": "АС",
                           "plaintiff": "П", "defendant": "Д"})
    p = tmp_path / "appeal.txt"
    p.write_text(payload["text"], encoding="utf-8")
    doc_id = _doc_add(env, case_id, p)
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "appeal_apk"], env=env)
    data = _j(out)["data"]
    assert data["checklist"]["Обжалуется"] == "found"
    assert data["checklist"]["Основания"] == "found"
    stripped = payload["text"].replace("## Обжалуется", "## Пусто")
    p.write_text(stripped, encoding="utf-8")
    doc_id2 = _doc_add(env, case_id, p)
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id2), "--kind", "appeal_apk"], env=env)
    data2 = _j(out)["data"]
    assert data2["checklist"]["Обжалуется"] == "missing"
    assert data2["verdict"] == "структурно неполный"


def test_checklist_json_envelope_shape(tmp_path):
    env, case_id = _mk_case(tmp_path, "A140-9/2026")
    doc_id = _doc_add(env, case_id, _skeleton(tmp_path))
    code, out, err = run_cli(
        ["--json", "case", "doc", "checklist", case_id, str(doc_id), "--kind", "hodat"], env=env)
    envelope = _j(out)
    assert envelope["status"] == "ok"
    assert envelope["meta"]["command"] == "case doc checklist"
