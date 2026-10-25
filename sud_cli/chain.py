"""the wave §1 (S-12): цепочка инстанций дела поверх VERIFIED deadline_matrix.

Границы (правило «тексты только из реестра»):
- ни одной даты «из воздуха»: точка без якорной базы = due_at=None +
  needs_anchor=True, ровно как в build_matrix (the wave) — движок переиспользуется,
  вторая копия логики якорения не заводится;
- порядок цепочки — НЕ нормативка, а процессуальная механика (кого за кем
  обжалуют). Он захардкожен константами CHAIN_ORDER_* ПО КЛЮЧАМ матрицы;
  в выводе стоит справочный disclaimer, обязывающий сверяться с нормой;
- cure-ключ (as_cure_left_without_movement) в цепочку НЕ входит: duration=null,
  он вне эскалации инстанций;
- ключ, которого нет в матрице этой юрисдикции, не выдумывается: он просто
  отсутствует, а расхождение попадёт в missing_from_matrix.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .deadline_matrix import INSTANCE_TO_JURISDICTION, build_matrix

#: Порядок эскалации по реальным ключам матрицы legal_rules.json (v1.4.0).
#: Арбитраж: решение -> апелляция (ч.1 ст.259 АПК) -> кассация округа
#: (ч.1 ст.276 АПК). Ключа кассации ВС в матрице нет — он не выдумывается.
CHAIN_ORDER_ARBITRATION: Tuple[str, ...] = (
    "as_appeal_decision",
    "as_cassation_district",
)
#: ГПК: решение -> апелляция (ст.321) -> кассация (ч.1 ст.376.1).
CHAIN_ORDER_GENERAL: Tuple[str, ...] = (
    "soyu_appeal_decision",
    "soyu_cassation",
)
#: КАС: решение -> апелляция (ч.1 ст.298) -> кассация (ч.2 ст.318).
CHAIN_ORDER_KAS: Tuple[str, ...] = (
    "kas_appeal_decision",
    "kas_cassation",
)

CHAIN_ORDER_BY_JURISDICTION: Dict[str, Tuple[str, ...]] = {
    "arbitration": CHAIN_ORDER_ARBITRATION,
    "general_jurisdiction": CHAIN_ORDER_GENERAL,
    "kas": CHAIN_ORDER_KAS,
}

#: Механическая метка стадии (не норма): поле выводится из порядка ключей.
STAGE_BY_KEY: Dict[str, str] = {
    "as_appeal_decision": "appeal",
    "as_cassation_district": "cassation_district",
    "soyu_appeal_decision": "appeal",
    "soyu_cassation": "cassation",
    "kas_appeal_decision": "appeal",
    "kas_cassation": "cassation",
}

#: Инстанции, перечисляемые при неизвестной instance дела.
KNOWN_INSTANCES: Tuple[str, ...] = ("apk", "gpk", "kas")

CHAIN_DISCLAIMER = (
    "Цепочка инстанций справочная: порядок точек — процессуальная механика "
    "sud-cli, а не норма; сроки и основания берутся из VERIFIED "
    "deadline_matrix, даты без якоря не выдумываются. Окончательный порядок "
    "эскалации определяет суд по тексту закона."
)

UNKNOWN_INSTANCE_NOTE = (
    "instance дела не задана (case field instance отсутствует) — показаны "
    "цепочки всех юрисдикций матрицы; выберите instance и повторите"
)

ANCHOR_HINT = "deadlines anchor <case_id> --key {key} --event-date <YYYY-MM-DD>"


def chain_keys_for_jurisdiction(jurisdiction: str, matrix: dict) -> List[str]:
    """Порядок цепочки, отфильтрованный по фактическим ключам матрицы.

    Ключа нет в матрице (или он не той юрисдикции) — в цепочку не попадает:
    выдумывать точку движок не имеет права.
    """
    order = CHAIN_ORDER_BY_JURISDICTION.get(jurisdiction, ())
    out = []
    for key in order:
        entry = matrix.get(key) or {}
        if entry.get("jurisdiction") == jurisdiction:
            out.append(key)
    return out


def missing_chain_keys(jurisdiction: str, matrix: dict) -> List[str]:
    """Ключи порядка, которых нет в матрице этой юрисдикции (пусто = норма)."""
    order = CHAIN_ORDER_BY_JURISDICTION.get(jurisdiction, ())
    present = set(chain_keys_for_jurisdiction(jurisdiction, matrix))
    return [k for k in order if k not in present]


def _point_blockers(nxt: Optional[dict]) -> List[str]:
    """Что блокирует переход дальше — только по фактам о самой точке цели.

    Здесь нет утверждений «по порядку инстанций следующая дата наступает после
    предыдущей»: матрица не хранит дату решения апелляции, и выводить её из
    соседней точки означало бы выдумать базу. Перечисляются ровно отсутствующие
    данные цели (якорь, состояние, due_at).
    """
    blocks: List[str] = []
    if nxt is None:
        return blocks
    if nxt.get("needs_anchor"):
        blocks.append(
            f"{nxt['key']}: база MISSING — срок не вычислен, нужен якорь "
            f"({ANCHOR_HINT.format(key=nxt['key'])})"
        )
    if nxt.get("state") is None:
        blocks.append(f"{nxt['key']}: состояние срока не зафиксировано")
    if nxt.get("due_at") is None and not nxt.get("needs_anchor"):
        # база есть, а due_at нет: движок не смог вычислить срок (duration=null,
        # отказ календаря) — это отдельный факт, а не «нужен якорь»
        blocks.append(f"{nxt['key']}: due_at не вычислен движком при наличии базы")
    return blocks


def _ordered_points(instance: str, matrix: dict, points_by_key: dict) -> List[dict]:
    jurisdiction = INSTANCE_TO_JURISDICTION.get(instance)
    chain = chain_keys_for_jurisdiction(jurisdiction, matrix) if jurisdiction else []
    ordered = []
    for i, key in enumerate(chain):
        point = points_by_key.get(key)
        if point is None:
            continue
        nxt = points_by_key.get(chain[i + 1]) if i + 1 < len(chain) else None
        enriched = {
            "key": key,
            "stage": STAGE_BY_KEY.get(key),
            "norm": point.get("norm"),
            "base_event": point.get("base_event"),
            "due_at": point.get("due_at"),
            "state": point.get("state"),
            "needs_anchor": bool(point.get("needs_anchor")),
            "is_last": nxt is None,
        }
        if point.get("warning_basis"):
            enriched["warning_basis"] = point["warning_basis"]
        enriched["blocks_next"] = _point_blockers(nxt)
        ordered.append(enriched)
    return ordered


#: Ключи, которые НЕ входят в цепочку эскалации, и почему. Формулировка —
#: описание трассы, а не норма: движок не утверждает, «как по закону», он
#: показывает, что этот ключ матрицы не является следующим звеном цепочки.
_OFF_CHAIN_REASON_MARKERS = (
    ("court_order", "трасса судебного приказа: отдельное производство, не эскалация решения"),
    ("simplified", "трасса упрощённого производства: своя апелляция, вне цепочки решения"),
    ("ruling", "обжалование определения: отдельная трасса, не звено цепочки по решению"),
    ("cure", "cure-точка (left without movement): duration=null, вне эскалации"),
    ("shortened_special", "специальный сокращённый срок: duration=null, назначает суд"),
)


def off_chain_keys(jurisdiction: str, matrix: dict) -> List[dict]:
    """Ключи матрицы этой юрисдикции, оставшиеся вне цепочки, с причиной.

    Нужно, чтобы «только ключи цепочки» не читалось как «остальное потеряно»:
    список полный, порядок — по ключу, причина — из морфологии ключа и duration.
    """
    in_chain = set(chain_keys_for_jurisdiction(jurisdiction, matrix))
    out = []
    for key in sorted(matrix):
        entry = matrix.get(key) or {}
        if entry.get("jurisdiction") != jurisdiction or key in in_chain:
            continue
        reason = "не является звеном цепочки эскалации решения"
        for marker, text in _OFF_CHAIN_REASON_MARKERS:
            if marker in key:
                reason = text
                break
        if entry.get("duration") is None and "duration=null" not in reason:
            reason = f"{reason}; duration=null — срок назначает суд"
        out.append({"key": key, "norm": entry.get("legal_basis"), "reason": reason})
    return out


def build_chain(
    state_dir: Any,
    case_id: str,
    instance_override: Optional[str] = None,
    rules: Optional[dict] = None,
) -> Dict[str, Any]:
    """Упорядоченная цепочка инстанций дела (или всех юрисдикций при unknown)."""
    instance, points = build_matrix(
        state_dir, case_id, instance_override=instance_override, rules=rules
    )
    from .legal_engine import load_rules

    matrix = (rules or load_rules()).get("deadline_matrix", {}) or {}
    by_key = {p["key"]: p for p in points}

    payload: Dict[str, Any] = {
        "case_id": case_id,
        "instance": instance,
        "chain": [],
        "chains_by_jurisdiction": [],
        "missing_from_matrix": [],
        "off_chain_keys": [],
        "disclaimer": CHAIN_DISCLAIMER,
    }

    if instance in INSTANCE_TO_JURISDICTION:
        jurisdiction = INSTANCE_TO_JURISDICTION[instance]
        payload["jurisdiction"] = jurisdiction
        payload["chain"] = _ordered_points(instance, matrix, by_key)
        payload["missing_from_matrix"] = missing_chain_keys(jurisdiction, matrix)
        payload["off_chain_keys"] = off_chain_keys(jurisdiction, matrix)
    else:
        # unknown instance: цепочка каждой юрисдикции матрицы, с пометкой.
        payload["note"] = UNKNOWN_INSTANCE_NOTE
        for inst in KNOWN_INSTANCES:
            juris = INSTANCE_TO_JURISDICTION[inst]
            payload["chains_by_jurisdiction"].append(
                {
                    "instance": inst,
                    "jurisdiction": juris,
                    "chain": _ordered_points(inst, matrix, by_key),
                    "missing_from_matrix": missing_chain_keys(juris, matrix),
                    "off_chain_keys": off_chain_keys(juris, matrix),
                }
            )
    return payload
