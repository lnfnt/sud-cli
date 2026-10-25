"""S-61 (Б) ручной контур ФССП — human-in-the-loop проверка БДИП.

Акт legal 2026.10.25 (проверено): официальный публичный API
ФССП отключён с 10.03.2022; обход капчи/лимитов запрещён. Утверждён базовый путь:
sud-cli готовит ссылку и чек-лист, капчу проходит человек с личного устройства,
результат фиксируется в досье как доказательство с provenance (кто/когда/чем
подтверждено). Внешние действия — за человеком (канон S-62).

Этот модуль НЕ выполняет сетевых запросов: только план проверки и оформление
результата. Данные ФССП носит ориентирующий характер (ст. 7 8-ФЗ о достоверности
оперативной информации): при споре подтверждать первоисточником/запросом.
"""
from __future__ import annotations

from typing import Optional

FSSP_BASE_URL = "https://fssp.gov.ru/iss/ip/"

_KINDS = {"fiz", "ur"}

CHECKLIST = [
    "Откройте ссылку на личном устройстве (не с серверного IP: ФССП лимитирует частые подключения).",
    "Выберите раздел: поиск по физическим лицам / юридических лицах.",
    "Введите параметры из плана ниже; при возможности сузьте регионом.",
    "Дождитесь ответа капчи/проверки; сделайте скриншот результата (или сохраните PDF).",
    "Вернитесь в sud-cli: fssp record — результат попадёт в реестр доказательств дела с отметкой provenance.",
    "Для процессуального использования: данные БДИП ориентирующие; при споре подтвердите запросом в ОСП/первоисточником.",
]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_plan(kind: str, *, name: Optional[str] = None, inn: Optional[str] = None,
               ogrn: Optional[str] = None, birth_date: Optional[str] = None,
               region: Optional[str] = None) -> dict:
    """Собрать план ручной проверки БДИП ФССП (без сетевых запросов)."""
    kind = (kind or "").strip().lower()
    _require(kind in _KINDS, f"kind must be one of {sorted(_KINDS)}")
    params: dict[str, str] = {}
    if kind == "fiz":
        _require(bool(name), "for kind=fiz provide --name (ФИО)")
        params["ФИО"] = str(name).strip()
        if birth_date:
            params["дата рождения"] = birth_date.strip()
    else:
        _require(bool(name or inn or ogrn),
                 "for kind=ur provide --name or --inn/--ogrn")
        if name:
            params["наименование"] = name.strip()
        if inn:
            params["ИНН"] = inn.strip()
        if ogrn:
            params["ОГРН"] = ogrn.strip()
    if region:
        params["регион"] = region.strip()
    return {
        "url": FSSP_BASE_URL,
        "kind": kind,
        "params": params,
        "checklist": list(CHECKLIST),
        "disclaimer": ("Данные Банка данных исполнительных производств носят ориентирующий характер; "
                       "проверку выполняет человек с личного устройства; результат фиксируется "
                       "в досье с provenance. Автоматический обход капчи/лимитов запрещён (юридическая проверка 25.10)."),
    }


def record_payload(*, subject: str, result: str, checked_by: str,
                   notes: Optional[str] = None,
                   evidence_file: Optional[str] = None) -> dict:
    """Оформить результат ручной проверки для реестра доказательств.

    title/note — без чужих ПДн pass-through сверх введённых самим оператором;
    файл доказательства (скрин/PDF) хранится отдельно, здесь только путь.
    """
    result = (result or "").strip().lower()
    _require(bool(subject and subject.strip()), "subject required")
    _require(bool(checked_by and checked_by.strip()), "checked_by required")
    _require(result in {"none", "found", "inconclusive"},
             "result must be none|found|inconclusive")
    verdict = {"none": "производств не найдено",
               "found": "производства найдены",
               "inconclusive": "проверка не завершена/неоднозначно"}[result]
    title = f"Ручная проверка ФССП БДИП: {subject.strip()}"
    note = f"Результат: {verdict}. Проверил(а): {checked_by.strip()}. Источник: {FSSP_BASE_URL} (ориентирующие данные)."
    if notes:
        note += f" Примечание: {notes.strip()}"
    return {"title": title, "note": note, "result": result,
            "evidence_file": evidence_file}
