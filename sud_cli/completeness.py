"""the wave §3 (S-71): структурный чек-лист комплектности — справка, не валиация.

Границы:
- правила живут в data/completeness_rules.json как СПИСОК СТРУКТУРНЫХ маркеров
  по kinds (isk_apk/claim_apk/appeal_apk/hodat); файл помечен format-checklist
  и несёт disclaimer: это НЕ юридическая проверка достаточности;
- ни в данных, ни в выводе не бывает нормативных цитат («ст.» с номером) —
  тесты гейтят это регуляркой по файлу и по выводу;
- вывод всегда exit 0: чек-лист справочный; бинарный файл без извлечённого
  текста = need-text hint, а не провал.
"""
import json
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "data" / "completeness_rules.json"

#: kinds правил; синхронизированы с forms.CHECKLIST_KIND_FOR_FORM (§4).
DEFAULT_KIND = "isk_apk"

VERDICT_FULL = "структурно полный"
VERDICT_INCOMPLETE = "структурно неполный"


def load_rules():
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    if rules.get("meta", {}).get("format") != "format-checklist":
        raise ValueError("completeness_rules.json: meta.format must be format-checklist")
    if not rules.get("meta", {}).get("disclaimer"):
        raise ValueError("completeness_rules.json: meta.disclaimer is required")
    return rules


def resolve_kind(kind_arg, title, rules):
    """kind документа: явный > совпадение в заголовке > DEFAULT_KIND.

    Никакой магии угадывания: если kind не передан и не читается из title,
    источник в выводе честно помечен 'default'.
    """
    kinds = rules.get("kinds", {})
    if kind_arg:
        if kind_arg not in kinds:
            raise ValueError(f"unknown checklist kind: {kind_arg}")
        return kind_arg, "explicit"
    text = (title or "").lower()
    for kind in kinds:
        if kind.lower() in text:
            return kind, "title"
    if "апелл" in text:
        return "appeal_apk", "title"
    return DEFAULT_KIND, "default"


def extract_text(path):
    """(text|None, hint). Plain-read utf-8; бинарь/нет файла -> None + hint."""
    p = Path(path)
    if not p.is_file():
        return None, "need-text: файл документа исчез с пути — список не извлечь"
    try:
        return p.read_text(encoding="utf-8"), None
    except (UnicodeDecodeError, ValueError):
        return None, ("need-text: файл не является plain-text (docx/pdf/скан) — "
                      "сначала извлеките текст (extract-text) и добавьте его как документ")


def evaluate(text, kind, rules):
    """Чек-лист {маркер: found|missing}: регистронезависимый подстрочный поиск."""
    hay = (text or "").casefold()
    markers = rules["kinds"][kind]
    checklist = {m: ("found" if m.casefold() in hay else "missing") for m in markers}
    missing = [m for m, state in checklist.items() if state == "missing"]
    return {
        "kind": kind,
        "checklist": checklist,
        "missing": missing,
        "verdict": VERDICT_INCOMPLETE if missing else VERDICT_FULL,
    }


def checklist_payload(doc, kind_arg, rules=None):
    """Полный конверт команды: данные + disclaimer; found/need_text — честные флаги."""
    rules = rules or load_rules()
    base = {
        "doc_id": doc.get("id") if doc else None,
        "case_id": doc.get("case_id") if doc else None,
        "path": doc.get("path") if doc else None,
        "title": doc.get("title") if doc else None,
        "disclaimer": rules["meta"]["disclaimer"],
        "format": rules["meta"]["format"],
    }
    if doc is None:
        base.update({"found": False, "need_text": False, "kind": None,
                     "kind_source": None, "checklist": {}, "missing": [],
                     "verdict": "документ не найден"})
        return base
    kind, kind_source = resolve_kind(kind_arg, doc.get("title"), rules)
    base.update({"found": True, "kind": kind, "kind_source": kind_source})
    text, hint = extract_text(doc.get("path"))
    if text is None:
        base.update({"need_text": True, "hint": hint, "checklist": {}, "missing": [],
                     "verdict": "текст не извлечён"})
        return base
    base["need_text"] = False
    base.update(evaluate(text, kind, rules))
    base["kind"] = kind
    return base
