"""v2.9.0 §1 — verify quote (S-52): правило «тексты только из реестра», «никогда не ok на глаз».

Offline-only: sources are the bundled npa editions (sud_cli/data), the local
events table and a plain-text file. No network, no live state dir.
"""

import json

import pytest

from sud_cli import mcp_server
from tests.conftest import run_cli

# Text of uk_rf art.159 as of 2015-11-20 = edition effective 2003-12-16.
NPA_159_TEXT = (
    "Мошенничество, то есть хищение чужого имущества или приобретение "
    "права на чужое имущество путем обмана или злоупотребления доверием."
)
NPA_SUBSTRING = "хищение чужого имущества"


def E(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def J(out):
    return json.loads(out)


def npa_args(text, act="uk_rf", article="159", date="2015-11-20", extra=()):
    return (["--json", "verify", "quote", "--text", text,
             "--source", "npa", "--act", act, "--article", article,
             "--date", date, *extra])


def write_file(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def file_args(tmp_path, text, path, extra=()):
    return (["--json", "verify", "quote", "--text", text,
             "--source", "file", "--path", str(path), *extra])


def import_case(tmp_path, doc_texts):
    events = []
    for i, t in enumerate(doc_texts, start=1):
        events.append({
            "event_id": f"E-{i}", "event_date": "2026-01-01",
            "event_type": "hearing", "doc_text": t,
            "source_name": "local-synthetic-json", "source_record_id": f"S-{i}",
        })
    case = {"case_id": "VC-1", "case_number": "А40-99/2026",
            "court": "Synthetic Example Court", "source_name": "local-synthetic-json",
            "events": events}
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps({"cases": [case]}, ensure_ascii=False), encoding="utf-8")
    code, out, err = run_cli(["--json", "court", "import", str(fixture)], env=E(tmp_path))
    assert code == 0, err
    return E(tmp_path)


# ---------------------------------------------------------------- npa source


def test_npa_exact(tmp_path):
    code, out, err = run_cli(npa_args(NPA_SUBSTRING), env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "exact"
    assert data["source"] == "npa"
    assert data["matched_fragment"] == NPA_SUBSTRING.lower()
    assert "uk_rf" in data["location"] and "159" in data["location"] and "2015-11-20" in data["location"]


def test_npa_full_text_exact(tmp_path):
    code, out, err = run_cli(npa_args(NPA_159_TEXT), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "exact"


def test_npa_not_found_no_fabrication(tmp_path):
    code, out, err = run_cli(npa_args("растрата чужого имущества"), env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "not_found"
    assert data["matched_fragment"] is None


def test_npa_edition_unavailable_unknown_act(tmp_path):
    code, out, err = run_cli(npa_args(NPA_SUBSTRING, act="kp_rf"), env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    # Missing bundle is NEVER reported as not_found.
    assert data["verdict"] == "edition_unavailable"


def test_npa_edition_unavailable_unknown_article(tmp_path):
    code, out, err = run_cli(npa_args(NPA_SUBSTRING, article="30"), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "edition_unavailable"


def test_npa_edition_unavailable_date_before_bundle(tmp_path):
    code, out, err = run_cli(npa_args(NPA_SUBSTRING, date="1990-01-01"), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "edition_unavailable"


# ---------------------------------------------------------------- case source


def test_case_exact_and_location_is_event_id(tmp_path):
    env = import_case(tmp_path, ["Определение о принятии заявления к производству."])
    code, out, err = run_cli(
        ["--json", "verify", "quote", "--text", "принятии заявления",
         "--source", "case", "--case-id", "VC-1"], env=env)
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "exact"
    assert data["location"] == "event:E-1"


def test_case_not_found(tmp_path):
    env = import_case(tmp_path, ["Определение о принятии заявления к производству."])
    code, out, err = run_cli(
        ["--json", "verify", "quote", "--text", "постановление о назначении экспертизы",
         "--source", "case", "--case-id", "VC-1"], env=env)
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "not_found"


# ---------------------------------------------------------------- file source


def test_file_exact_offset_shown(tmp_path):
    path = write_file(tmp_path, "doc.txt", "Первая строка.\nВторая строка про мошенничество.")
    code, out, err = run_cli(file_args(tmp_path, "про мошенничество", path), env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "exact"
    # Normalized body: "первая строка. вторая строка про мошенничество."
    assert data["offset"] == 29
    assert data["location"].startswith("file:")
    assert str(data["offset"]) in data["location"]


def test_nbsp_normalized_to_space(tmp_path):
    path = write_file(tmp_path, "nbsp.txt", "хищение\xa0чужого\xa0имущества путём обмана")
    code, out, err = run_cli(file_args(tmp_path, "хищение чужого имущества", path), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "exact"


def test_yo_e_case_insensitive(tmp_path):
    path = write_file(tmp_path, "yo.txt", "Мошенничество; счёт в банке отменён.")
    code, out, err = run_cli(file_args(tmp_path, "счет в банке отменен", path), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "exact"


def test_punctuation_is_not_stripped_never_ok_on_eye(tmp_path):
    # Comma in source, absent in query: normalization keeps punctuation, so a
    # visual "looks the same" match must NOT be reported as exact.
    path = write_file(tmp_path, "comma.txt", "хищение, имущества")
    code, out, err = run_cli(file_args(tmp_path, "хищения имущества", path), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "not_found"


def test_fuzzy_disabled_by_default(tmp_path):
    src = "Мошенничество, то есть хищение чужого имущества путем обмана."
    q = "Мошенничество, то есть присвоение чужого имущества путем обмана."
    path = write_file(tmp_path, "fz.txt", src)
    code, out, err = run_cli(file_args(tmp_path, q, path), env=E(tmp_path))
    assert code == 0, err
    assert J(out)["data"]["verdict"] == "not_found"


def test_fuzzy_above_threshold_returns_ratio_and_snippet(tmp_path):
    src = "Мошенничество, то есть хищение чужого имущества путем обмана."
    q = "Мошенничество, то есть присвоение чужого имущества путем обмана."
    path = write_file(tmp_path, "fz.txt", src)
    code, out, err = run_cli(file_args(tmp_path, q, path, extra=["--max-fuzzy", "0.2"]), env=E(tmp_path))
    assert code == 0, err
    data = J(out)["data"]
    assert data["verdict"] == "fuzzy"
    assert data["ratio"] >= 0.8
    assert data["threshold"] == pytest.approx(0.8)
    assert data["snippet"] and "присвоение".lower() not in data["snippet"]
    assert "хищение" in data["snippet"]


# ---------------------------------------------------------------- validation


def test_empty_text_invalid_arguments(tmp_path):
    code, out, err = run_cli(npa_args(""), env=E(tmp_path))
    assert code != 0
    payload = J(err)["data"]
    assert payload["status"] == "INVALID_ARGUMENTS"


def test_missing_source_params_invalid(tmp_path):
    code, out, err = run_cli(
        ["--json", "verify", "quote", "--text", "что-то", "--source", "npa"], env=E(tmp_path))
    assert code != 0
    assert J(err)["data"]["status"] == "INVALID_ARGUMENTS"


def test_max_fuzzy_out_of_range_invalid(tmp_path):
    path = write_file(tmp_path, "f.txt", "любой текст")
    code, out, err = run_cli(file_args(tmp_path, "любой", path, extra=["--max-fuzzy", "1.5"]), env=E(tmp_path))
    assert code != 0
    assert J(err)["data"]["status"] == "INVALID_ARGUMENTS"


def test_file_missing_invalid(tmp_path):
    code, out, err = run_cli(
        ["--json", "verify", "quote", "--text", "x", "--source", "file",
         "--path", str(tmp_path / "nope.txt")], env=E(tmp_path))
    assert code != 0
    assert J(err)["data"]["status"] == "INVALID_ARGUMENTS"


def test_file_not_utf8_invalid(tmp_path):
    p = tmp_path / "bin.txt"
    p.write_bytes(b"\xff\xfe not valid utf-8")
    code, out, err = run_cli(
        ["--json", "verify", "quote", "--text", "x", "--source", "file",
         "--path", str(p)], env=E(tmp_path))
    assert code != 0
    assert J(err)["data"]["status"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------- MCP tool


def test_mcp_verify_quote_registered_with_strict_schema():
    names = [t["name"] for t in mcp_server.tool_list()["tools"]]
    assert "sud_cli_verify_quote" in names
    schema = next(t for t in mcp_server.TOOLS if t["name"] == "sud_cli_verify_quote")["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"text", "source"}
    assert schema["properties"]["source"]["enum"] == ["npa", "case", "file"]


def test_mcp_verify_quote_exact_npa():
    value = mcp_server._call("sud_cli_verify_quote", {
        "text": NPA_SUBSTRING, "source": "npa",
        "act": "uk_rf", "article": "159", "date": "2015-11-20"})
    assert value["verdict"] == "exact"


def test_mcp_verify_quote_file_source(tmp_path):
    path = write_file(tmp_path, "m.txt", "Точная цитата для MCP.")
    value = mcp_server._call("sud_cli_verify_quote", {
        "text": "точная цитата", "source": "file", "path": str(path)})
    assert value["verdict"] == "exact"
    assert value["offset"] == 0


def test_case_doc_attached_exact(tmp_path):
    """Контроль 24.10: приложенный case doc обязан сканироваться — не только events."""
    e = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", "А40-500/2026",
                              "--court", "АС", "--plaintiff", "И.", "--defendant", "О."], env=e)
    cid = json.loads(out)["data"]["case_id"]
    doc = tmp_path / "opred.txt"
    doc.write_text("Производство по делу прекращено без рассмотрения.\n", encoding="utf-8")
    code, out, err = run_cli(["--json", "case", "doc", "add", cid, "--path", str(doc)], env=e)
    assert code == 0, err
    code, out, err = run_cli(["--json", "verify", "quote", "--source", "case",
                              "--case-id", cid, "--text", "прекращено без рассмотрения"], env=e)
    assert code == 0, err
    d = json.loads(out)["data"]
    assert d["verdict"] == "exact" and d["location"].startswith("doc:")
    assert d["docs_scanned"] == 1


def test_case_doc_binary_skipped_honestly(tmp_path):
    e = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", "А40-501/2026",
                              "--court", "АС", "--plaintiff", "И.", "--defendant", "О."], env=e)
    cid = json.loads(out)["data"]["case_id"]
    binf = tmp_path / "scan.pdf"
    binf.write_bytes(b"%PDF-1.7\x00\xff\xfe binary garbage")
    run_cli(["--json", "case", "doc", "add", cid, "--path", str(binf)], env=e)
    code, out, err = run_cli(["--json", "verify", "quote", "--source", "case",
                              "--case-id", cid, "--text", "что угодно"], env=e)
    d = json.loads(out)["data"]
    assert d["verdict"] == "not_found"
    assert d["docs_skipped_unreadable"] == 1
    assert "НЕ проверялись" in d["detail"]
