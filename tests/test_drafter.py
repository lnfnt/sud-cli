# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Тесты генератора процессуальных документов (the task brief).

sud-cli draft / sud_cli_draft: 7 шаблонов, Law Zero валидация
(missing_required_fields, exit 2), сборка .md/.docx (TNR 12pt, автор
sud-cli), связка с движками fees/interest, MCP-инструмент.
"""
import json
from pathlib import Path

import pytest

from sud_cli.drafter import DOCX_AUTHOR, TEMPLATES, _money, draft_document, templates_report
from tests.conftest import run_cli

CLAIM_FIELDS = dict(
    court="АС г. Москвы",
    plaintiff="ООО «Алёша»",
    defendant="АО «Бета»",
    claim_amount="2500000",
    basis="поставлен товар, всё ещё не оплачен",
    petitum="взыскать долг и проценты",
)


# ---------------------------------------------------------------------------
# Шаблоны
# ---------------------------------------------------------------------------

def test_templates_list_has_seven_types_with_required_fields():
    report = templates_report()
    types = [t["type"] for t in report]
    assert types == [
        "claim_arbitration", "claim_soyu", "objections", "petition_expert",
        "petition_interim", "petition_deferral", "appeal",
    ]
    for entry in report:
        assert entry["label"] and entry["law"]
        assert entry["required"], entry["type"]
        for name in entry["required"]:
            assert name in TEMPLATES[entry["type"]]["required"]


def test_cli_draft_templates_subcommand():
    code, out, err = run_cli(["--json", "draft", "templates"])
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "ok"
    assert len(data["data"]) == 7
    assert data["data"][0]["type"] == "claim_arbitration"


# ---------------------------------------------------------------------------
# Law Zero: валидация обязательных реквизитов
# ---------------------------------------------------------------------------

def test_missing_required_fields_rejects_assembly():
    res = draft_document("claim_arbitration", court="АС г. Москвы", plaintiff="ООО «Альфа»")
    assert res["status"] == "missing_required_fields"
    assert set(res["missing"]) == {"defendant", "claim_amount", "basis", "petitum"}
    assert "markdown" not in res, "сборка заблокирована — документа нет"


def test_cli_missing_required_fields_exits_code_2():
    code, out, err = run_cli(
        ["--json", "draft", "--type", "claim_arbitration", "--court", "АС г. Москвы"]
    )
    assert code == 2, (out, err)
    data = json.loads(err)  # ошибки идут в stderr, stdout остаётся чистым
    assert data["status"] == "error"
    assert data["data"]["status"] == "missing_required_fields"
    assert "defendant" in data["data"]["missing"]


def test_unknown_doc_type_fails_closed():
    from sud_cli import mcp_server
    with pytest.raises(ValueError):
        draft_document("complaint_echr")
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sud_cli_draft", "arguments": {"doc_type": "complaint_echr"}}})
    assert resp["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Генерация иска в АС: Markdown и .docx
# ---------------------------------------------------------------------------

def test_claim_arbitration_markdown_structure():
    res = draft_document("claim_arbitration", **CLAIM_FIELDS)
    assert res["status"] == "ok"
    md = res["markdown"]
    assert "В АС г. Москвы" in md
    assert "Истец: ООО «Алёша»" in md
    assert "Ответчик: АО «Бета»" in md
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ" in md
    assert "ПРОШУ:\nвзыскать долг и проценты" in md
    assert "Приложения:" in md and "Квитанция об уплате государственной пошлины" in md


def test_claim_arbitration_docx_metadata_and_layout(tmp_path):
    out = tmp_path / "claim.docx"
    res = draft_document("claim_arbitration", out=out, **CLAIM_FIELDS)
    assert res["status"] == "ok" and res["format"] == "docx"
    import docx

    document = docx.Document(out)
    assert document.core_properties.author == DOCX_AUTHOR == "sud-cli"
    style = document.styles["Normal"]
    assert style.font.name == "Times New Roman"
    assert style.font.size.pt == 12
    assert style.paragraph_format.line_spacing == 1.5
    assert document.sections[0].left_margin.cm == pytest.approx(3.0, abs=0.01)
    assert document.sections[0].right_margin.cm == pytest.approx(1.5, abs=0.01)
    assert document.sections[0].top_margin.cm == pytest.approx(2.0, abs=0.01)
    assert document.sections[0].bottom_margin.cm == pytest.approx(2.0, abs=0.01)

    text = "\n".join(p.text for p in document.paragraphs)
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ" in text
    # «ё» вычищена в описательной части...
    assert "все еще не оплачен" in text and "всё" not in text
    # ...но официальные реквизиты сторон не тронуты
    assert "ООО «Алёша»" in text


def test_cli_draft_md_out_detects_format_by_extension(tmp_path):
    out = tmp_path / "claim.md"
    code, out_s, err = run_cli([
        "--json", "draft", "--type", "claim_arbitration",
        "--court", "АС г. Москвы", "--plaintiff", "ООО «Альфа»",
        "--defendant", "АО «Бета»", "--claim-amount", "2500000",
        "--basis", "не оплачен товар", "--petitum", "взыскать", "-o", str(out),
    ])
    assert code == 0, (out_s, err)
    data = json.loads(out_s)
    assert data["status"] == "ok" and data["data"]["format"] == "md"
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ" in out.read_text(encoding="utf-8")


def test_cli_draft_case_file_autofill(tmp_path):
    case_file = tmp_path / "case.json"
    case_file.write_text(json.dumps({
        "court": "АС г. Москвы", "plaintiff": "ООО «Альфа»", "defendant": "АО «Бета»",
        "claim_amount": "2500000", "basis": "не оплачен товар", "petitum": "взыскать долг",
    }, ensure_ascii=False), encoding="utf-8")
    code, out, err = run_cli([
        "--json", "draft", "--type", "claim_arbitration", "--case-file", str(case_file),
    ])
    assert code == 0, (out, err)
    data = json.loads(out)
    assert data["status"] == "ok"
    assert "ООО «Альфа»" in data["data"]["markdown"]


def test_cli_draft_case_id_from_registry(tmp_path):
    from sud_cli.db import create_case, get_connection, init_db

    state = tmp_path / "state"
    init_db(state)
    conn = get_connection(state)
    try:
        res = create_case(conn, "А40-12345/2026", "АС г. Москвы", "ООО «Альфа»", "АО «Бета»")
    finally:
        conn.close()
    code, out, err = run_cli([
        "--json", "--state-dir", str(state),
        "draft", "--type", "claim_arbitration", "--case-id", res["case_id"],
        "--claim-amount", "2500000", "--basis", "не оплачен товар", "--petitum", "взыскать долг",
    ])
    assert code == 0, (out, err)
    data = json.loads(out)
    assert data["status"] == "ok"
    md = data["data"]["markdown"]
    assert "Дело № А40-12345/2026" in md and "АО «Бета»" in md


# ---------------------------------------------------------------------------
# Связка с движками: fees + interest
# ---------------------------------------------------------------------------

def test_fee_and_interest_integration():
    res = draft_document(
        "claim_arbitration", **CLAIM_FIELDS,
        interest_start="2025-01-01", interest_end="2025-07-01",
    )
    assert res["status"] == "ok"
    fee, interest = res["fee"], res["interest"]
    assert fee and fee["status"] == "ok" and fee["amount_rub"] > 0
    assert interest and interest["status"] == "ok" and interest["amount_rub"] > 0
    md = res["markdown"]
    assert "основной долг: 2 500 000,00 руб." in md
    assert "проценты по ст. 395 ГК РФ" in md
    assert _money(interest["amount_rub"]) + " руб." in md
    assert _money(fee["amount_rub"]) + " руб." in md


def test_fee_integrated_without_interest(tmp_path):
    out = tmp_path / "claim.docx"
    res = draft_document("claim_arbitration", out=out, **CLAIM_FIELDS)
    assert res["fee"]["status"] == "ok" and res["interest"] is None
    assert "Госпошлина (ст. 333.21 НК РФ)" in res["markdown"]


# ---------------------------------------------------------------------------
# Остальные 6 шаблонов собираются
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "doc_type,fields",
    [
        ("claim_soyu", dict(court="Ленинский районный суд", plaintiff="Иванов И.И.",
                            defendant="Петров П.П.", claim_amount="150000",
                            basis="продан товар", petitum="взыскать")),
        ("objections", dict(court="АС г. Москвы", case_number="А40-12345/2026",
                            plaintiff="ООО Альфа", defendant="ООО Бета",
                            basis="требования необоснованны", petitum="отказать в иске")),
        ("petition_expert", dict(court="АС г. Москвы", case_number="А40-12345/2026",
                                 basis="спор о качестве товара", questions="соответствует ли товар ГОСТ",
                                 petitum="назначить строительно-техническую экспертизу")),
        ("petition_interim", dict(court="АС г. Москвы", case_number="А40-12345/2026",
                                  basis="ответчик выводит активы", measure="арест счетов",
                                  petitum="запретить распоряжение денежными средствами")),
        ("petition_deferral", dict(court="АС г. Москвы", plaintiff="ООО Альфа",
                                   claim_amount="2500000", basis="тяжелое имущественное положение")),
        ("appeal", dict(court="9 ААС", case_number="А40-12345/2026",
                        basis="суд не применил ст. 395 ГК РФ", petitum="отменить решение")),
    ],
)
def test_all_templates_assemble(doc_type, fields):
    res = draft_document(doc_type, **fields)
    assert res["status"] == "ok", res
    md = res["markdown"]
    assert "ПРОШУ" in md and "Приложения:" in md
    assert res["doc_type"] == doc_type


def test_petition_deferral_default_petitum_and_fee():
    res = draft_document("petition_deferral", court="АС г. Москвы", plaintiff="ООО Альфа",
                         claim_amount="2500000", reason="недостаточность средств")
    assert res["status"] == "ok"
    assert "отсрочить уплату государственной пошлины" in res["markdown"]
    assert res["fee"]["status"] == "ok" and res["fee"]["amount_rub"] > 0


# ---------------------------------------------------------------------------
# MCP: sud_cli_draft
# ---------------------------------------------------------------------------

def test_mcp_tool_count_is_38_and_draft_present():
    from sud_cli import mcp_server

    tools = mcp_server.tool_list()["tools"]
    assert len(tools) == 38, f"ожидалось 38 инструментов (+1 sud_cli_draft, +1 sud_cli_plenum_search), получено {len(tools)}"
    assert "sud_cli_draft" in {t["name"] for t in tools}
    schema = next(t["inputSchema"] for t in tools if t["name"] == "sud_cli_draft")
    assert schema["required"] == ["doc_type"]


def test_mcp_draft_call_ok():
    from sud_cli import mcp_server

    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "sud_cli_draft", "arguments": {
            "doc_type": "claim_arbitration", "court": "АС г. Москвы",
            "plaintiff": "ООО «Альфа»", "defendant": "АО «Бета»",
            "claim_amount": 2500000, "basis": "не оплачен товар",
            "petitum": "взыскать долг"}}})
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["status"] == "ok"
    assert body["fee"]["amount_rub"] > 0
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ" in body["markdown"]


def test_mcp_draft_missing_fields_is_error_free_response():
    from sud_cli import mcp_server

    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "sud_cli_draft", "arguments": {
            "doc_type": "objections", "court": "АС"}}})
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["status"] == "missing_required_fields"
    assert "case_number" in body["missing"]


# ---------------------------------------------------------------------------
# Фидбэк практикующего юриста (дело о расторжении ДКП): override-поля,
# представитель, блок подписи, длинная фабула, basis списком.
# ---------------------------------------------------------------------------

DKP_FIELDS = dict(
    court="Ленинский районный суд г. Екатеринбурга",
    plaintiff="Смирнов Алексей Петрович",
    defendant="ООО «СтройИнвест»",
    claim_amount="3850000",
    basis=[
        "06.03.2025 между истцом и ответчиком заключен договор купли-продажи "
        "квартиры № 14 по адресу: г. Екатеринбург, ул. Примерная, д. 1.",
        "Истец полностью исполнил обязательство по оплате, однако ответчик "
        "переданием квартиры по акту приема-передачи до настоящего времени "
        "уклоняется, что свидетельствует о существенном нарушении договора.",
    ],
    petitum=("расторгнуть договор купли-продажи от 06.03.2025, взыскать "
             "уплаченные денежные средства в размере 3 850 000 руб., проценты "
             "по ст. 395 ГК РФ."),
    title="ИСКОВОЕ ЗАЯВЛЕНИЕ\nо расторжении договора купли-продажи и взыскании уплаченных денежных средств",
    normative=("В соответствии со ст. 309, 310 ГК РФ обязательства должны "
               "исполняться надлежащим образом. В силу ст. 450 п. 2 ГК РФ "
               "односторонний отказ от исполнения договора допускается при "
               "существенном нарушении договора одной из сторон. На основании "
               "ст. 450.2 ГК РФ при расторжении договора каждая из сторон "
               "вправе требовать возврата исполненного."),
    appendix=[
        "1. Квитанция об уплате государственной пошлины.",
        "2. Уведомление о направлении копии искового заявления ответчику.",
        "3. Договор купли-продажи от 06.03.2025.",
        "4. Платежные поручения об оплате договорной цены.",
        "5. Переписка сторон о передаче квартиры.",
        "6. Доверенность представителя (ст. 131 ч. 2 п. 2 ГПК РФ).",
    ],
    representative="адвокат Колесников Д.В.",
)


def test_title_normative_appendix_overrides():
    res = draft_document("claim_soyu", **DKP_FIELDS)
    assert res["status"] == "ok"
    md = res["markdown"]
    assert "о расторжении договора купли-продажи" in md
    assert "о взыскании денежных средств в размере" not in md
    assert "ст. 450 п. 2 ГК РФ" in md
    assert "Расчет взыскиваемой суммы приведен ниже" not in md
    assert "Переписка сторон о передаче квартиры" in md
    # generic-список шаблона заменен полностью
    assert "Выписки из ЕГРЮЛ" not in md


def test_defaults_unchanged_without_overrides():
    res = draft_document("claim_arbitration", **CLAIM_FIELDS)
    md = res["markdown"]
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ\nо взыскании денежных средств" in md
    assert "ст. 309, 310 ГК РФ" in md
    assert "Выписки из ЕГРЮЛ в отношении сторон" in md
    # подпись по умолчанию — истец (эталон 07.11.2026: роль + Ф.И.О., снизу дата)
    assert "«      » __________ 20__ г." in md
    assert "Представитель" in md and "ООО «Алёша»" in md


def test_representative_in_header_general_jurisdiction():
    res = draft_document("claim_soyu", **{k: v for k, v in DKP_FIELDS.items()
                                          if k != "title"})
    md = res["markdown"]
    assert "Представитель по доверенности: адвокат Колесников Д.В. (ст. 131 ч. 2 п. 2 ГПК РФ)" in md


def test_representative_in_header_arbitration_no_gpk_cite():
    res = draft_document("claim_arbitration", **CLAIM_FIELDS,
                         representative="Иванов И.И.")
    md = res["markdown"]
    assert "Представитель по доверенности: Иванов И.И." in md
    assert "ст. 131 ч. 2 п. 2 ГПК РФ" not in md


def test_signature_block_default_from_plaintiff_and_representative(tmp_path):
    out = tmp_path / "dkp.docx"
    res = draft_document("claim_soyu", out=out, **DKP_FIELDS)
    assert res["status"] == "ok"
    md = res["markdown"]
    # дата + оба подписанта по умолчанию (эталон 07.11.2026)
    assert "«      » __________ 20__ г." in md
    assert "Смирнов Алексей Петрович" in md
    assert "адвокат Колесников Д.В. (по доверенности)" in md

    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    document = docx.Document(out)
    paragraphs = [p for p in document.paragraphs if p.text.strip()]
    sig_paragraphs = [p for p in paragraphs if "«      »" in p.text or "Смирнов" in p.text]
    assert sig_paragraphs, "блок подписи отсутствует в docx"
    assert all(p.alignment == WD_ALIGN_PARAGRAPH.LEFT for p in sig_paragraphs)
    # блок подписи замыкает документ: подписанты, затем дата
    assert "Смирнов Алексей Петрович" in paragraphs[-3].text
    assert "адвокат Колесников Д.В. (по доверенности)" in paragraphs[-2].text
    assert "«      »" in paragraphs[-1].text


def test_signature_override_replaces_default():
    res = draft_document("claim_arbitration", **CLAIM_FIELDS,
                         signature=["Директор Иванов И.И.", "Бухгалтер Петрова А.А."])
    md = res["markdown"]
    assert "Директор Иванов И.И." in md
    assert "Бухгалтер Петрова А.А." in md
    assert "/ ООО «Алёша» /" not in md, "override полностью заменяет состав подписантов"


def test_basis_long_fabula_over_4500_chars():
    long_basis = (
        "Истец и ответчик заключили договор купли-продажи недвижимого имущества. "
        "Истец исполнил обязанность по оплате в полном объеме, о чем свидетельствуют "
        "банковские платежные документы. "
    ) * 30  # > 4500 знаков, < лимита 10000
    assert 4500 < len(long_basis) < 10000
    res = draft_document("claim_arbitration", **{**CLAIM_FIELDS, "basis": long_basis})
    assert res["status"] == "ok"
    assert long_basis.strip() in res["markdown"]


def test_basis_limit_still_enforced_over_10000():
    with pytest.raises(ValueError, match="лимит"):
        draft_document("claim_arbitration", **{**CLAIM_FIELDS, "basis": "а" * 10001})


def test_basis_as_list_of_paragraphs():
    res = draft_document("claim_soyu", **DKP_FIELDS)
    md = res["markdown"]
    assert "заключен договор купли-продажи" in md
    # абзацы фабулы разделены пустой строкой
    assert "06.03.2025 между истцом" in md
    assert "уклоняется, что свидетельствует о существенном нарушении договора." in md


def test_basis_list_total_length_limited():
    paragraphs = ["а" * 6000, "б" * 6000]
    with pytest.raises(ValueError, match="лимит"):
        draft_document("claim_arbitration", **{**CLAIM_FIELDS, "basis": paragraphs})


def test_cli_draft_new_override_flags(tmp_path):
    out = tmp_path / "dkp.md"
    code, out_s, err = run_cli([
        "--json", "draft", "--type", "claim_soyu",
        "--court", "Ленинский районный суд", "--plaintiff", "Иванов И.И.",
        "--defendant", "Петров П.П.", "--claim-amount", "3850000",
        "--basis", "не передана квартира по ДКП",
        "--petitum", "расторгнуть договор",
        "--representative", "адвокат Колесников Д.В.",
        "--title", "ИСКОВОЕ ЗАЯВЛЕНИЕ\nо расторжении договора купли-продажи",
        "--normative", "ст. 450, 450.2 ГК РФ",
        "--appendix", "1. Договор купли-продажи.",
        "--appendix", "2. Платежные поручения.",
        "--signature", "Иванов И.И.",
        "-o", str(out),
    ])
    assert code == 0, (out_s, err)
    md = out.read_text(encoding="utf-8")
    assert "о расторжении договора купли-продажи" in md
    assert "ст. 450, 450.2 ГК РФ" in md
    assert "Представитель по доверенности: адвокат Колесников Д.В." in md
    assert "1. Договор купли-продажи." in md
    assert "2. Платежные поручения." in md
    assert "Представитель" in md and "Иванов И.И." in md


def test_mcp_draft_new_fields_accepted_by_schema_gate():
    from sud_cli import mcp_server

    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "sud_cli_draft", "arguments": {
            "doc_type": "claim_soyu", "court": "Ленинский районный суд",
            "plaintiff": "Иванов И.И.", "defendant": "Петров П.П.",
            "claim_amount": 3850000, "basis": ["абзац один", "абзац два"],
            "petitum": "расторгнуть договор",
            "title": "ИСКОВОЕ ЗАЯВЛЕНИЕ\nо расторжении договора купли-продажи",
            "representative": "адвокат Колесников Д.В.",
            "appendix": ["1. Договор."],
        }}})
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["status"] == "ok"
    assert "о расторжении договора купли-продажи" in body["markdown"]
    assert "абзац один" in body["markdown"] and "абзац два" in body["markdown"]
