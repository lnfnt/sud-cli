# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Генератор процессуальных документов: sud-cli draft / sud_cli_draft.

Law Zero (brief colleague 2026-10-28): отсутствие любого обязательного
реквизита блокирует сборку — возвращается структурированный отказ
``missing_required_fields`` со списком недостающих полей. Пустых полей,
заполнителей и домысленных реквизитов не бывает: в документ попадает только
то, что явно передано, плюс нормативный каркас и приложения шаблона.

Оформление .docx (ГОСТ/делопроизводство): Times New Roman 14 pt,
полуторный интервал, красная строка 1.25 см, поля 3.0/1.0/2.0/2.0 см,
автор метаданных ``bortsov``, буква «ё» вычищается в описательной части
(кроме официальных реквизитов сторон).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

DOCX_AUTHOR = "bortsov"

#: Канонический порядок типов — как в БЛОКЕ 1 спецификации юриста.
TEMPLATES = {
    "claim_arbitration": {
        "label": "Исковое заявление в арбитражный суд",
        "law": "ст. 125, 126 АПК РФ",
        "required": ["court", "plaintiff", "defendant", "claim_amount", "basis", "petitum"],
        "is_claim": True,
        "jurisdiction": "arbitration",
        "fee_kind": "property_claim",
        "fee_ref": "ст. 333.21 НК РФ",
        "normative": (
            "В соответствии со ст. 309, 310 ГК РФ обязательства должны исполняться "
            "надлежащим образом. Проценты за пользование чужими денежными средствами "
            "начисляются по ст. 395 ГК РФ. Расчет взыскиваемой суммы приведен ниже."
        ),
        "appendix": [
            "1. Квитанция об уплате государственной пошлины.",
            "2. Уведомление о направлении копии искового заявления ответчику.",
            "3. Доверенность представителя.",
            "4. Выписки из ЕГРЮЛ в отношении сторон.",
            "5. Расчет суммы иска.",
            "6. Документы, подтверждающие обстоятельства, на которые ссылается истец.",
        ],
    },
    "claim_soyu": {
        "label": "Исковое заявление в суд общей юрисдикции",
        "law": "ст. 131, 132 ГПК РФ",
        "required": ["court", "plaintiff", "defendant", "claim_amount", "basis", "petitum"],
        "is_claim": True,
        "jurisdiction": "general_jurisdiction",
        "fee_kind": "property_claim",
        "fee_ref": "ст. 333.19 НК РФ",
        "normative": (
            "В соответствии со ст. 309, 310 ГК РФ обязательства должны исполняться "
            "надлежащим образом. Проценты за пользование чужими денежными средствами "
            "начисляются по ст. 395 ГК РФ. Расчет взыскиваемой суммы приведен ниже."
        ),
        "appendix": [
            "1. Квитанция об уплате государственной пошлины.",
            "2. Уведомление о направлении копии искового заявления ответчику.",
            "3. Доверенность представителя.",
            "4. Расчет суммы иска.",
            "5. Документы, подтверждающие обстоятельства, на которые ссылается истец.",
        ],
    },
    "objections": {
        "label": "Отзыв (возражения) на исковое заявление",
        "law": "ст. 131 АПК РФ / ст. 149 ГПК РФ",
        "required": ["court", "case_number", "plaintiff", "defendant", "basis", "petitum"],
        "is_claim": False,
        "jurisdiction": "arbitration",
        "fee_kind": "property_claim",
        "fee_ref": "",
        "normative": (
            "Отзыв на исковое заявление подготовлен в порядке ст. 131 АПК РФ / "
            "ст. 149 ГПК РФ. Возражения по каждому доводу иска приведены ниже."
        ),
        "appendix": [
            "1. Доказательства направления отзыва лицам, участвующим в деле.",
            "2. Документы, обосновывающие возражения.",
        ],
    },
    "petition_expert": {
        "label": "Ходатайство о назначении судебной экспертизы",
        "law": "ст. 82 АПК РФ / ст. 79 ГПК РФ",
        "required": ["court", "case_number", "basis", "petitum"],
        "is_claim": False,
        "jurisdiction": "arbitration",
        "fee_kind": "property_claim",
        "fee_ref": "",
        "normative": (
            "В соответствии со ст. 82 АПК РФ / ст. 79 ГПК РФ для разъяснения "
            "возникающих при рассмотрении дела вопросов, требующих специальных "
            "познаний, назначается экспертиза."
        ),
        "appendix": [
            "1. Сведения об экспертном учреждении (наименование, адрес, ИНН, квалификация экспертов, стоимость и сроки проведения).",
            "2. Подтверждение внесения средств на депозитный счет суда.",
        ],
    },
    "petition_interim": {
        "label": "Ходатайство об обеспечении иска",
        "law": "ст. 90-92 АПК РФ / ст. 139-140 ГПК РФ",
        "required": ["court", "case_number", "basis", "petitum"],
        "is_claim": False,
        "jurisdiction": "arbitration",
        "fee_kind": "property_claim",
        "fee_ref": "",
        "normative": (
            "В соответствии со ст. 90-92 АПК РФ / ст. 139-140 ГПК РФ суд вправе "
            "принять срочные временные меры по обеспечению иска."
        ),
        "appendix": [
            "1. Документы, подтверждающие основания и соразмерность обеспечительных мер.",
            "2. Встречное обеспечение (при наличии).",
        ],
    },
    "petition_deferral": {
        "label": "Заявление об отсрочке уплаты госпошлины",
        "law": "ст. 333.41 НК РФ",
        "required": ["court", "plaintiff", "claim_amount", "reason"],
        "is_claim": False,
        "jurisdiction": "arbitration",
        "fee_kind": "property_claim",
        "fee_ref": "ст. 333.21 НК РФ",
        "normative": (
            "На основании ст. 333.41 НК РФ и ст. 102 АПК РФ суд вправе "
            "предоставить отсрочку или рассрочку уплаты государственной пошлины."
        ),
        "appendix": [
            "1. Справка ИФНС об открытых банковских счетах.",
            "2. Справки банков об остатках денежных средств.",
            "3. Справки банков об отсутствии картотеки (неисполненных расчетных документов).",
        ],
    },
    "appeal": {
        "label": "Апелляционная жалоба",
        "law": "ст. 259, 260 АПК РФ / ст. 320-322 ГПК РФ",
        "required": ["court", "case_number", "basis", "petitum"],
        "is_claim": False,
        "jurisdiction": "arbitration",
        "fee_kind": "appeal",
        "fee_ref": "ст. 333.21 НК РФ",
        "normative": (
            "В соответствии со ст. 259, 260 АПК РФ решение, не вступившее в "
            "законную силу, может быть обжаловано в апелляционном порядке."
        ),
        "appendix": [
            "1. Документ об уплате государственной пошлины.",
            "2. Доказательства направления копии жалобы лицам, участвующим в деле.",
            "3. Доверенность представителя.",
        ],
    },
}

#: Ключи JSON case-file / registry card, подтягиваемые в документ.
CASE_FILE_KEYS = (
    "court", "plaintiff", "defendant", "claim_amount", "basis", "petitum",
    "case_number", "pretrial", "reason", "measure", "questions", "experts",
    "counter_security", "interest_start", "interest_end", "applicant_type",
)

#: Допустимые аргументы draft_document (fail-closed: лишний ключ — TypeError).
KNOWN_FIELDS = frozenset(CASE_FILE_KEYS)

_DOCX_KINDS = {
    # kind: (alignment, red-line 1.25 cm, right-side requisites indent)
    "header": ("left", False, False),
    "req": ("left", False, True),
    "title": ("center", False, False),
    "body": ("justify", True, False),
    "law": ("justify", True, False),
    "calc": ("left", True, False),
    "petitum": ("justify", True, False),
    "appendix": ("left", False, False),
}

_BODY_KINDS = frozenset({"body", "law", "calc", "petitum", "appendix"})


def _clean_yo(text: str) -> str:
    """«ё»/«Ё» вычищаются в описательной части (реквизиты не трогаем)."""
    return text.replace("ё", "е").replace("Ё", "Е")


def _money(value) -> str:
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{dec:,.2f}".replace(",", " ").replace(".", ",")


def _to_decimal(value):
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("claim_amount must be a number of rubles, not a boolean")
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"claim_amount must be a number of rubles, got {value!r}") from None


def _effective(name, values):
    """reason ← reason или basis (в CLI отдельного флага --reason нет)."""
    if name == "reason":
        return values.get("reason") or values.get("basis")
    return values.get(name)


MAX_FIELD_LENGTH = 2000


def _clean_field(name, value):
    """S3-15: нормализация и лимиты полей дела перед сборкой документа.

    Строка очищается от краевых пробелов, CRLF приводится к LF, длина
    ограничена MAX_FIELD_LENGTH. Пустая после strip строка — отсутствующее
    поле (Law Zero: пустых реквизитов не бывает).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > MAX_FIELD_LENGTH:
        raise ValueError(
            f"поле '{name}' превышает лимит {MAX_FIELD_LENGTH} символов"
        )
    return cleaned


def templates_report() -> list[dict]:
    """Список поддерживаемых типов и их обязательных полей (`draft templates`)."""
    return [
        {"type": key, "label": tpl["label"], "law": tpl["law"], "required": tpl["required"]}
        for key, tpl in TEMPLATES.items()
    ]


def load_case_fields(case_id=None, case_file=None, state_dir=None) -> dict:
    """Автоподтягивание реквизитов из реестра дел (--case-id) или JSON (--case-file)."""
    if case_id and case_file:
        raise ValueError("укажите один источник: --case-id или --case-file")
    if case_file:
        data = json.loads(Path(case_file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("case-file JSON должен быть объектом с реквизитами")
        return {key: data[key] for key in CASE_FILE_KEYS if data.get(key) is not None}
    if case_id:
        from .db import get_case, get_connection, init_db

        state_dir = Path(state_dir) if state_dir else Path.home() / ".local" / "share" / "sud-cli"
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            case = get_case(conn, case_id)
        finally:
            conn.close()
        if case is None:
            raise ValueError(f"case not found: {case_id}")
        fields = {}
        for key in ("court", "plaintiff", "defendant"):
            if case.get(key):
                fields[key] = case[key]
        if case.get("case_number"):
            fields["case_number"] = case["case_number"]
        return fields
    return {}


def _title(doc_type, values, total_str):
    if doc_type in ("claim_arbitration", "claim_soyu"):
        return f"ИСКОВОЕ ЗАЯВЛЕНИЕ\nо взыскании денежных средств в размере {total_str} руб."
    if doc_type == "objections":
        return f"ОТЗЫВ НА ИСКОВОЕ ЗАЯВЛЕНИЕ\nпо делу № {values['case_number']}"
    if doc_type == "petition_expert":
        return "ХОДАТАЙСТВО\nо назначении судебной экспертизы"
    if doc_type == "petition_interim":
        return "ХОДАТАЙСТВО\nо принятии обеспечительных мер"
    if doc_type == "petition_deferral":
        return "ЗАЯВЛЕНИЕ\nоб отсрочке уплаты государственной пошлины"
    return f"АПЕЛЛЯЦИОННАЯ ЖАЛОБА\nна решение по делу № {values['case_number']}"


def _fabula(doc_type, values):
    basis = values.get("basis")
    lines = []
    if doc_type in ("claim_arbitration", "claim_soyu"):
        lines.append(str(basis))
    elif doc_type == "objections":
        lines.append(
            "Ответчик не признает исковые требования в заявленном размере и "
            f"возражает по следующим основаниям:\n{basis}"
        )
    elif doc_type == "petition_expert":
        lines.append(f"Обстоятельства, требующие специальных познаний: {basis}")
        if values.get("questions"):
            lines.append(f"Вопросы, предлагаемые эксперту: {values['questions']}")
        if values.get("experts"):
            lines.append(f"Кандидатуры экспертных учреждений: {values['experts']}")
    elif doc_type == "petition_interim":
        lines.append(f"Обоснование угрозы неисполнения решения суда: {basis}")
        if values.get("measure"):
            lines.append(f"Испрашиваемая мера обеспечения: {values['measure']}")
        if values.get("counter_security"):
            lines.append(f"Встречное обеспечение: {values['counter_security']}")
    elif doc_type == "petition_deferral":
        reason = values.get("reason") or values.get("basis")
        lines.append(
            f"В связи с {reason} немедленная уплата государственной пошлины затруднительна."
        )
    else:  # appeal
        lines.append(f"Основания апелляционного обжалования: {basis}")
    return "\n".join(lines)


def _calc_section(doc_type, tpl, values, fee, interest, total_str):
    lines = []
    if doc_type in ("claim_arbitration", "claim_soyu"):
        lines.append("Расчет взыскиваемой суммы:")
        lines.append(f"основной долг: {_money(values['claim_amount'])} руб.")
        if interest:
            lines.append(
                f"проценты по ст. 395 ГК РФ за период {values['interest_start']} - "
                f"{values['interest_end']} ({interest['days']} дн.): "
                f"{_money(interest['amount_rub'])} руб."
            )
            for period in interest.get("periods", []):
                lines.append(
                    f"с {period['from']} по {period['to']} ({period['days']} дн., "
                    f"ставка {period['rate']}%): {period['amount_rub']} руб."
                )
            lines.append(f"итого цена иска: {total_str} руб.")
        else:
            lines.append(f"итого цена иска: {total_str} руб.")
    elif interest or fee:
        lines.append("Расчетная часть:")
    if fee and fee.get("status") == "ok" and fee.get("amount_rub") is not None:
        if doc_type == "petition_deferral":
            lines.append(
                f"Государственная пошлина по иску на сумму "
                f"{_money(values['claim_amount'])} руб. составляет "
                f"{_money(fee['amount_rub'])} руб. (расчет sud-cli fees)."
            )
        else:
            lines.append(
                f"Госпошлина ({tpl['fee_ref']}): {_money(fee['amount_rub'])} руб."
            )
    return "\n".join(lines)


def _build_sections(doc_type, tpl, values, fee, interest, total_str):
    parts = [("header", f"В {values['court']}")]

    requisites = []
    if values.get("case_number"):
        requisites.append(f"Дело № {values['case_number']}")
    if doc_type in ("claim_arbitration", "claim_soyu", "objections"):
        requisites.append(f"Истец: {values['plaintiff']}")
        requisites.append(f"Ответчик: {values['defendant']}")
    else:
        if values.get("plaintiff"):
            requisites.append(f"Заявитель: {values['plaintiff']}")
        if values.get("defendant"):
            requisites.append(f"Ответчик: {values['defendant']}")
    parts.append(("req", "\n".join(requisites)))

    parts.append(("title", _title(doc_type, values, total_str)))
    parts.append(("body", _fabula(doc_type, values)))

    law_lines = [tpl["normative"]]
    if values.get("pretrial"):
        law_lines.append(f"Досудебный (претензионный) порядок: {values['pretrial']}.")
    parts.append(("law", "\n".join(law_lines)))

    calc = _calc_section(doc_type, tpl, values, fee, interest, total_str)
    if calc:
        parts.append(("calc", calc))

    petitum = values.get("petitum")
    if doc_type == "petition_deferral" and not petitum:
        petitum = "отсрочить уплату государственной пошлины до вступления решения суда в законную силу."
    parts.append(("petitum", f"На основании изложенного ПРОШУ:\n{petitum}"))
    parts.append(("appendix", "Приложения:\n" + "\n".join(tpl["appendix"])))

    return [(kind, _clean_yo(text) if kind in _BODY_KINDS else text) for kind, text in parts]


def _markdown(sections) -> str:
    return "\n\n".join(text for _, text in sections) + "\n"


def build_docx(sections, path):
    """Собрать .docx: TNR 14pt/1.5/отступ 1.25 см, поля 3/1/2/2, автор bortsov."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    align_map = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }
    document = Document()
    document.core_properties.author = DOCX_AUTHOR
    page = document.sections[0]
    page.left_margin = Cm(3.0)
    page.right_margin = Cm(1.0)
    page.top_margin = Cm(2.0)
    page.bottom_margin = Cm(2.0)

    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(14)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.first_line_indent = Cm(1.25)
    normal.paragraph_format.space_after = Pt(0)
    rfonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), "Times New Roman")

    for kind, text in sections:
        align, want_first, req_side = _DOCX_KINDS.get(kind, ("left", False, False))
        for line in text.split("\n") or [""]:
            paragraph = document.add_paragraph()
            paragraph.alignment = align_map[align]
            pf = paragraph.paragraph_format
            pf.line_spacing = 1.5
            pf.first_line_indent = Cm(1.25) if want_first else Cm(0)
            if req_side:
                pf.left_indent = Cm(9.0)
            run = paragraph.add_run(line)
            run.font.name = "Times New Roman"
            run.font.size = Pt(14)
            run.font.bold = kind == "title"
    document.save(str(path))


def draft_document(doc_type, out=None, doc_format=None, **fields) -> dict:
    """Собрать процессуальный документ; вернуться может только ok или missing_required_fields."""
    tpl = TEMPLATES.get(doc_type)
    if tpl is None:
        raise ValueError(
            f"unknown doc_type: {doc_type!r}; допустимые типы: {', '.join(TEMPLATES)}"
        )
    values = dict(fields)
    unknown = sorted(set(values) - KNOWN_FIELDS)
    if unknown:
        raise TypeError(f"unknown draft arguments: {unknown}")
    # S3-15: нормализация/лимиты до проверки обязательности — whitespace-only
    # и структурная injection (CRLF, неограниченная длина) в реквизиты не проходят.
    for key in list(values):
        values[key] = _clean_field(key, values[key])

    out_path = Path(out) if out is not None else None
    if doc_format is None and out_path is not None:
        doc_format = "docx" if out_path.suffix.lower() == ".docx" else "md"
    if doc_format is None:
        doc_format = "md"
    if doc_format not in ("md", "docx"):
        raise ValueError(f"unknown format: {doc_format!r}")
    if doc_format == "docx" and out_path is None:
        raise ValueError("docx требует путь итогового файла (--out/-o)")

    missing = [
        name for name in tpl["required"]
        if (_effective(name, values) is None
            or (isinstance(_effective(name, values), str)
                and not _effective(name, values).strip()))
    ]
    if missing:
        return {
            "status": "missing_required_fields",
            "doc_type": doc_type,
            "missing": missing,
            "required": tpl["required"],
            "detail": (
                f"Сборка заблокирована (Law Zero): отсутствуют обязательные "
                f"реквизиты: {', '.join(missing)}"
            ),
        }

    claim_amount = _to_decimal(values.get("claim_amount"))
    values["claim_amount"] = claim_amount

    start_s = values.get("interest_start")
    end_s = values.get("interest_end")
    if bool(start_s) != bool(end_s):
        raise ValueError("interest_start и interest_end передаются вместе")
    interest = None
    if start_s:
        if claim_amount is None:
            raise ValueError("расчет процентов требует claim_amount")
        from .interest import calculate_interest

        calc = calculate_interest(claim_amount, date.fromisoformat(start_s), date.fromisoformat(end_s))
        if calc.status != "ok":
            raise ValueError(f"расчет процентов отклонен: {calc.status}")
        interest = calc.to_dict()

    fee = None
    if claim_amount is not None:
        from .legal_engine import calculate_fee, load_rules

        fee = calculate_fee(
            rules=load_rules(),
            claim_amount=claim_amount,
            jurisdiction=tpl["jurisdiction"],
            fee_kind=tpl["fee_kind"],
            applicant_type=values.get("applicant_type") or "entity",
        ).to_dict()

    total = claim_amount if claim_amount is not None else Decimal("0")
    if interest:
        total += Decimal(str(interest["amount_rub"]))
    total_str = _money(total)

    sections = _build_sections(doc_type, tpl, values, fee, interest, total_str)
    markdown = _markdown(sections)
    if out_path is not None:
        if doc_format == "docx":
            build_docx(sections, out_path)
        else:
            out_path.write_text(markdown, encoding="utf-8")

    return {
        "status": "ok",
        "doc_type": doc_type,
        "template": {"label": tpl["label"], "law": tpl["law"], "required": tpl["required"]},
        "format": doc_format,
        "output_path": str(out_path) if out_path is not None else None,
        "fee": fee,
        "interest": interest,
        "markdown": markdown,
    }
