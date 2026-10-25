"""Ручной контур ЕФРСБ (act 25.10 §1.2): human-in-the-loop fallback.

Боевой автоматизированный путь проверки банкротства — КАД (kad-search).
ЕФРСБ закрыт за Qrator/WAF; автономный browser-адаптер запрещён актом (ст. 272 УК РФ).
Модуль не делает сетевых запросов: формирует валидную поисковую ссылку, которую
ОТКРЫВАЕТ ЧЕЛОВЕК в основном браузере, и интерфейс фиксации provenance в досье дела.
"""
from __future__ import annotations

from typing import Optional

BASE_URL = "https://bankrot.fedresurs.ru/"
_METHOD = "efrsb_human_verified"  # не сеть: человек + запись реквизитов

KINDS = ("ul", "ip", "fl")


class EfrsbManualError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise EfrsbManualError(msg)


def link_plan(kind: str, *, name: Optional[str] = None, inn: Optional[str] = None,
              ogrn: Optional[str] = None, birth_date: Optional[str] = None) -> dict:
    """Собрать план ручной проверки ЕФРСБ (без сетевых запросов, акт 25.10)."""
    kind = (kind or "").lower()
    _require(kind in KINDS, f"kind должен быть одним из {KINDS}")
    if kind == "fl":
        _require(bool(name), "для физлица нужно ФИО (--name)")
        query = name
        params = {"debtorFirstName": name}
    elif kind == "ip":
        _require(bool(name or inn), "для ИП нужно имя или ИНН")
        query = inn or name
        params = {"inn": inn} if inn else {"debtorLastName": name}
    else:
        _require(bool(name or inn or ogrn), "для ЮЛ нужно имя/ИНН/ОГРН")
        query = inn or ogrn or name
        params = {"inn": inn} if inn else ({"ogrn": ogrn} if ogrn else {"name": name})

    # Предзаполнение: официальный интерфейс поиска принимает строковый запрос;
    # ссылка остаётся на главную — человек вводит запрос в защищённой форме (Qrator).
    steps = [
        f"Открой в основном браузере: {BASE_URL}",
        f"Раздел «Поиск» → должник: {query}",
        "Пройди капчу/проверку — это делает ТОЛЬКО человек",
        "Сохрани карточку должника/публикации (номер, дата, вид сообщения)",
        "Зафиксируй в досье: sud-cli efrsb record --case <ID> --subject ... --result ... --checked-by <кто>",
    ]
    return {
        "kind": kind,
        "base_url": BASE_URL,
        "query": query,
        "search_params": {k: v for k, v in params.items() if v},
        "steps": steps,
        "automation": "off (act 25.10: browser-адаптер запрещён; КАД — боевой путь, ЕФРСБ — ручная перепроверка)",
        "primary_instead": "kad-search (дела о несостоятельности, категория Б) — автоматизировано",
    }


def record_payload(*, subject: str, result: str, checked_by: str,
                   source_ref: Optional[str] = None,
                   notes: Optional[str] = None) -> dict:
    """Provenance-запись результата ручной проверки ЕФРСБ (аналог fssp record)."""
    _require(bool(subject), "нужен --subject (ИНН/ФИО/дело)")
    _require(result in ("found", "none", "inconclusive"),
             "result должен быть found|none|inconclusive")
    _require(bool(checked_by), "нужен --checked-by (кто проверял)")
    note_bits = [f"method={_METHOD}"]
    if source_ref:
        note_bits.append(f"source_ref={source_ref}")
    if notes:
        note_bits.append(notes)
    return {
        "subject": subject,
        "result": result,
        "title": f"ЕФРСБ вручную: {subject} — {result}",
        "note": " ".join(note_bits),
        "checked_by": checked_by,
        "source": "bankrot.fedresurs.ru (человек)",
    }
