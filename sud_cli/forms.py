"""the wave §4 (S-70): структурные скелеты процессуальных форм + bus-free docx-команда.

Границы:
- только структурные скелеты с плейсхолдерами [ФИО]/[дело]/[дата] и данными
  дела из реестра (обезличенно: case_number/court/plaintiff/defendant);
- НИКАКИХ нормативных цитат: в модуле нет ни одной ссылки «ст.» с номером,
  и тесты гейтят это regex'ом и по исходнику, и по выводу;
- sud никогда не запускает docx-конвертер: docx_command собирает готовую
  shlex-экранированную строку (паттерн reminders/alerts) и пишет spec-файл;
  модуль не импортирует ничего, что умеет поднимать внешние процессы;
- deferral-шаблон госпошлины НЕ дублируется: вид otsrochka даёт структуру и
  ссылку-команду на существующий генератор `sud-cli deferral`.

SYNC ТОЧКА §3<->§4: заголовки markdown-секций ниже = маркеры
data/completeness_rules.json (hodat: Шапка/Просьба/Подпись/Дата;
appeal: +Обжалуется/Основания/Приложения). Меняешь маркер — меняй заголовок,
тесты test_v2120_3 расходятся первыми.
"""
import json
import shlex
import tempfile
from pathlib import Path

FORM_KINDS = ("vosstanovlenie_sroka", "otsrochka", "appeal_structure")

#: вид формы -> checklist-kind из completeness_rules.json (единый набор маркеров)
CHECKLIST_KIND_FOR_FORM = {
    "vosstanovlenie_sroka": "hodat",
    "otsrochka": "hodat",
    "appeal_structure": "appeal_apk",
}

BUS_FREE_NOTE = (
    "bus-free: sud-cli не запускает docx-конвертер; ниже готовая экранированная "
    "строка команды — исполнение остаётся за оператором"
)

VERIFY_CMD_TEMPLATE = '/usr/bin/sud-cli form new {kind} --case {case_id} --format md'


def _case_fields(case):
    case = case or {}
    return {
        "case_id": case.get("case_id"),
        "case_number": case.get("case_number") or "[дело]",
        "court": case.get("court") or "[суд]",
        "plaintiff": case.get("plaintiff") or "[истец]",
        "defendant": case.get("defendant") or "[ответчик]",
    }


def _header(c):
    return (
        "## Шапка\n"
        f"В [суд]: {c['court']}\n"
        f"Дело [дело] N {c['case_number']}\n"
        f"[истец]: {c['plaintiff']}\n"
        f"[ответчик]: {c['defendant']}\n"
    )


def _skeleton_vosstanovlenie(c):
    return (
        "# Ходатайство о восстановлении процессуального срока (структурный скелет)\n\n"
        + _header(c)
        + "\n## Просьба\n"
        "Прошу восстановить пропущенный срок для [действие].\n"
        "[причина пропуска]: ...; [момент, с которым связан срок]: ...\n"
        "[доказательства уважительности]: ...\n\n"
        "## Подпись\n"
        "[ФИО] ________________________\n\n"
        "## Дата\n"
        "[ДД.ММ.ГГГГ]\n"
    )


def _skeleton_otsrochka(c):
    return (
        "# Ходатайство об отсрочке (рассрочке) уплаты государственной пошлины (структурный скелет)\n\n"
        + _header(c)
        + "\n## Просьба\n"
        "Прошу предоставить отсрочку (рассрочку) уплаты пошлины в размере [сумма] до [ДД.ММ.ГГГГ].\n"
        "[обстоятельства имущественного положения]: ...\n\n"
        "## Приложения\n"
        "[справка банка об остатках]\n[иные документы]\n\n"
        "## Подпись\n"
        "[ФИО] ________________________\n\n"
        "## Дата\n"
        "[ДД.ММ.ГГГГ]\n\n"
        "---\n"
        "Существующий шаблон ходатайства об отсрочке пошлины НЕ дублируется: "
        "полный текст даёт команда `sud-cli deferral --applicant ... --court ... "
        "--amount ... --reason ...`.\n"
    )


def _skeleton_appeal(c):
    return (
        "# Апелляционная жалоба (структурный скелет)\n\n"
        + _header(c)
        + "\n## Обжалуется\n"
        "Судебный акт от [ДД.ММ.ГГГГ] по делу N " + str(c["case_number"]) + ".\n\n"
        "## Основания\n"
        "[довод 1]: ...\n[довод 2]: ...\n\n"
        "## Просьба\n"
        "Прошу [решение по результату рассмотрения].\n\n"
        "## Приложения\n"
        "[копия судебного акта]\n[документы, подтверждающие доводы]\n\n"
        "## Подпись\n"
        "[ФИО] ________________________\n\n"
        "## Дата\n"
        "[ДД.ММ.ГГГГ]\n"
    )


_BUILDERS = {
    "vosstanovlenie_sroka": _skeleton_vosstanovlenie,
    "otsrochka": _skeleton_otsrochka,
    "appeal_structure": _skeleton_appeal,
}


def render_skeleton(kind, case):
    if kind not in _BUILDERS:
        raise ValueError(f"unknown form kind: {kind}")
    return _BUILDERS[kind](_case_fields(case))


def _as_sections(kind, case):
    """json-представление: список {heading, body} без потери структуры md."""
    text = render_skeleton(kind, case)
    sections = []
    current = None
    for line in text.splitlines():
        if line.startswith("#"):
            if current:
                sections.append(current)
            current = {"heading": line.lstrip("# ").strip(), "body": ""}
        elif current is not None:
            current["body"] = (current["body"] + "\n" + line).strip("\n")
    if current:
        sections.append(current)
    return sections


def render_form(kind, case, fmt="md"):
    """Формы: markdown|json. Только чтение данных дела, без сайд-эффектов."""
    if fmt not in ("md", "json"):
        raise ValueError(f"unknown format: {fmt}")
    skeleton = render_skeleton(kind, case)
    if fmt == "md":
        return {
            "status": "ok",
            "kind": kind,
            "format": "md",
            "checklist_kind": CHECKLIST_KIND_FOR_FORM[kind],
            "case": _case_fields(case),
            "text": skeleton,
        }
    return {
        "status": "ok",
        "kind": kind,
        "format": "json",
        "checklist_kind": CHECKLIST_KIND_FOR_FORM[kind],
        "case": _case_fields(case),
        "sections": _as_sections(kind, case),
    }


def build_docx_spec(kind, case):
    return {
        "generator": "sud-cli form docx-command",
        "kind": kind,
        "case": _case_fields(case),
        "content_md": render_skeleton(kind, case),
    }


def docx_command(kind, case, spec_dir=None):
    """Готовая shlex-строка docx-cli build; sud НИКОГДА не вызывает её сам.

    Паттерн reminders/alerts: сборка строки + запись spec-файла. Никаких
    внешних процессов: исполнение — осознанный шаг оператора.
    """
    case_id = _case_fields(case)["case_id"] or "[case]"
    spec = build_docx_spec(kind, case)
    if spec_dir is None:
        spec_dir = Path(tempfile.mkdtemp(prefix="sud-form-"))
    else:
        spec_dir = Path(spec_dir)
        spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{kind}-{case_id}-spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    verify = VERIFY_CMD_TEMPLATE.format(kind=kind, case_id=case_id)
    parts = ["docx-cli", "build", "--spec", str(spec_path), "--verify", verify, "--verify-run"]
    command = " ".join(shlex.quote(p) for p in parts)
    return {
        "status": "ok",
        "kind": kind,
        "command": command,
        "spec_path": str(spec_path),
        "verify_command": verify,
        "bus_free": BUS_FREE_NOTE,
    }
