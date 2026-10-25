"""история §5 (S-07): права субъекта — маш-читаемый экспорт дела и полное уничтожение.

Границы:
- НИКАКИХ нормативных текстов от себя: основания, срок хранения и перечень
  прав берутся дословно из блока ``pdn_policy`` правил (legal_rules.json,
  v1.4.0 финальная) — строки не печатаются по памяти и не хардкодятся;
- экспорт = данные для субъекта/юриста: полные, без выдуманных сокращений и
  без «маскирования на глаз»; audit-записи по делу включаются как есть,
  потому что вырезание части данных — это и есть сокращение;
- purge = уничтожение: каскад строк всех таблиц дела + физические файлы из
  case_documents/evidence, а в audit — только ФАКТ (id/дата/actor/счётчики).
  ПДн в audit не попадает: ни имён сторон, ни текстов, ни путей файлов.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .db import (
    PURGE_TABLES,
    case_file_paths,
    case_row_counts,
    get_case,
    get_case_card,
    get_connection,
    init_db,
    list_case_documents,
    purge_case,
    read_deadline_audit,
    verify_case_evidence,
    evidence_summary,
)

EXPORT_FORMATS = ("json", "md")

#: Что именно выгружается — список секций печатается в конверте, чтобы
#: приёмщик видел покрытие, а не догадывался о нём.
EXPORT_SECTIONS = (
    "case",
    "events",
    "amounts",
    "documents",
    "evidence",
    "representatives",
    "deadline_states",
    "notes",
    "audit",
)


def pdn_policy(rules: Optional[dict] = None) -> Dict[str, Any]:
    """Блок pdn_policy из правил (дословно, без правок и без дописываний)."""
    if rules is None:
        from .legal_engine import load_rules

        rules = load_rules()
    return rules.get("pdn_policy", {}) or {}


def pdn_reference_block(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Ссылочный блок для конвертов: только поля pdn_policy из правил."""
    return {
        "source": "legal_rules.json:pdn_policy",
        "legal_status": policy.get("legal_status"),
        "grounds": list(policy.get("grounds") or []),
        "retention": policy.get("retention"),
        "subject_rights": list(policy.get("subject_rights") or []),
    }


def export_case(state_dir: Any, case_id: str, fmt: str = "json",
                rules: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Полный дамп дела + ссылочный блок pdn_policy (дословно из правил)."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unknown format: {fmt} (допустимо: {', '.join(EXPORT_FORMATS)})")
    if rules is None:
        from .legal_engine import load_rules

        rules = load_rules()
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        if get_case(conn, case_id) is None:
            return None
        card = get_case_card(conn, case_id)
        docs = list_case_documents(conn, case_id)
        evidence_items = verify_case_evidence(conn, case_id)
        summary = evidence_summary(evidence_items)
        rows = case_row_counts(conn, case_id)
        audit = read_deadline_audit(state_dir, case_id)
    finally:
        conn.close()

    policy = pdn_policy(rules)
    payload: Dict[str, Any] = {
        "case_id": case_id,
        "format": fmt,
        "sections": list(EXPORT_SECTIONS),
        "row_counts": {t: rows.get(t, 0) for t in PURGE_TABLES},
        "case": card.get("case"),
        "parties": card.get("parties"),
        "events": card.get("events"),
        "amounts": card.get("amounts"),
        "documents": docs,
        "evidence": {
            "items": evidence_items,
            "summary": {k: v for k, v in summary.items() if k != "problem_items"},
        },
        "representatives": card.get("representatives"),
        "deadline_states": card.get("deadlines"),
        "notes": card.get("notes"),
        # audit включается целиком: вырезание записей = сокращение данных,
        # а экспорт для субъекта/юриста по определению без сокращений.
        "audit": audit,
        "pdn": pdn_reference_block(policy),
        "completeness_note": (
            "Дамп содержит все строки реестра по этому делу: ни одна секция не "
            "сокрощена, не «обезличена на лету» и не усечена лимитом. Числа "
            "row_counts — фактическое количество удалённых/выгруженных строк, "
            "а не оценка."
        ),
    }
    return payload


def render_export_md(payload: Dict[str, Any]) -> str:
    """md-рендер дампа: заголовки секций + таблицы/списки из фактических строк."""
    case = payload.get("case") or {}
    lines: List[str] = [
        f"# Экспорт дела {payload['case_id']}",
        "",
        f"- номер: {case.get('case_number')}",
        f"- суд: {case.get('court')}",
        f"- статус: {case.get('status')}",
        f"- instance: {case.get('instance')}",
        f"- owner: {case.get('owner')}",
        f"- истец: {(payload.get('parties') or {}).get('plaintiff')}",
        f"- ответчик: {(payload.get('parties') or {}).get('defendant')}",
        "",
        "## Покрытие (row_counts)",
        "",
        "| таблица | строк |",
        "| --- | --- |",
    ]
    for table in PURGE_TABLES:
        lines.append(f"| {table} | {payload['row_counts'].get(table, 0)} |")

    def _block(title: str, items: Any) -> None:
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("_пусто_")
            return
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if isinstance(item, dict):
                for key in sorted(item):
                    value = item[key]
                    if value is None or value == "" or value == []:
                        continue
                    lines.append(f"- **{key}**: {value}")
                lines.append("")
            else:
                lines.append(f"- {item}")

    _block("События", payload.get("events"))
    _block("Суммы", payload.get("amounts"))
    _block("Документы (с хэшами)", payload.get("documents"))
    _block("Доказательства", (payload.get("evidence") or {}).get("items"))
    _block("Представители", (payload.get("representatives") or {}).get("items"))
    _block("Состояния сроков", payload.get("deadline_states"))
    _block("Заметки", payload.get("notes"))
    _block("Журнал правок сроков (audit)", payload.get("audit"))

    pdn = payload.get("pdn") or {}
    lines += [
        "",
        "## ПДн: основания и срок (дословно из pdn_policy правил)",
        "",
        f"Источник: {pdn.get('source')}",
        f"Статус блока: {pdn.get('legal_status')}",
        "",
        "Основания обработки:",
    ]
    lines += [f"- {g}" for g in (pdn.get("grounds") or [])] or ["- (в правилах нет)"]
    lines += ["", "Срок хранения:", f"- {pdn.get('retention')}", "",
              "Права субъекта:"]
    lines += [f"- {r}" for r in (pdn.get("subject_rights") or [])] or ["- (в правилах нет)"]
    lines += ["", payload.get("completeness_note", "")]
    return "\n".join(lines) + "\n"


def purge_preview(state_dir: Any, case_id: str,
                  rules: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Превью уничтожения: счётчики строк + файлы + retention-hint из правил."""
    if rules is None:
        from .legal_engine import load_rules

        rules = load_rules()
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        if get_case(conn, case_id) is None:
            return None
        counts = case_row_counts(conn, case_id)
        files = case_file_paths(conn, case_id)
    finally:
        conn.close()
    policy = pdn_policy(rules)
    return {
        "case_id": case_id,
        "will_delete_rows": {t: counts.get(t, 0) for t in PURGE_TABLES},
        "will_delete_files": files,
        "file_count": len(files),
        "audit_after_purge": (
            "в audit попадёт только запись о факте: case id, дата, actor, "
            "счётчики; содержимое дела в журнал не пишется"
        ),
        "retention_hint": policy.get("retention"),
        "pdn": pdn_reference_block(policy),
        "requires": "--yes (деструктив без подтверждения не исполняется)",
    }


def purge_apply(state_dir: Any, case_id: str, confirmed: bool = False,
                actor: str = "cli", rules: Optional[dict] = None,
                remove_files: bool = True) -> Dict[str, Any]:
    """Уничтожение дела. Без confirmed — только превью, ноль изменений.

    remove_files=False (--keep-files) оставляет строки case_documents нетронутыми
    по отношению к диску: файлы чужие (общий каталог сканов), а строк дела это
    не меняет — каскад таблиц всё равно полный.
    """
    if rules is None:
        from .legal_engine import load_rules

        rules = load_rules()
    preview = purge_preview(state_dir, case_id, rules=rules)
    if preview is None:
        return {"status": "not_found", "case_id": case_id}
    if not confirmed:
        return {
            "status": "needs_confirmation",
            "detail": "purge уничтожает данные дела безвозвратно: нужен --yes",
            "preview": preview,
        }
    init_db(state_dir)
    conn = get_connection(state_dir)
    try:
        result = purge_case(conn, state_dir, case_id, actor=actor,
                            remove_files=remove_files)
    finally:
        conn.close()
    result["status"] = "purged"
    result["files_kept"] = not remove_files
    result["retention_hint"] = preview["retention_hint"]
    result["pdn"] = preview["pdn"]
    return result
