"""Варианты представления вывода (render styles).

Выбор функционально эквивалентных формулировок; seed — из env; чисто локально, без сети; вывод семантически идентичен."""
from __future__ import annotations

import hashlib
import hmac
import os

_ENV_SEED = "SUD_BUILD_SEED"
_ENV_SECRET = "SUD_BUILD_SECRET"

# вариантные столбцы: кортежи эквивалентных формулировок (смысл/числа неизменны)
STYLE_VARIANTS: dict[str, tuple[str, ...]] = {
    "status_hint": (
        "sud не утверждает жизненный статус нормы/акта без источника: проверьте "
        "актуальность через Camofox/egress прогон или manual_review; verdict=unknown "
        "означает отсутствие локальной записи, а не подтверждённое действие",
        "без источника sud не выдаёт суждение о жизненном статусе нормы/акта: сверьте "
        "актуальность Camofox/egress прогоном или manual_review; verdict=unknown — это "
        "отсутствие локальной записи, а не подтверждение действия",
        "sud не присваивает норме/акту жизненный статус без источника: актуальность "
        "проверяется Camofox/egress прогоном либо manual_review; verdict=unknown значит "
        "нет локальной записи, а не то, что действие подтверждено",
    ),
    "egrul_basis": (
        "справочная карточка из открытых данных ФНС (ст. 6 129-ФЗ)",
        "карточка справок сформирована из открытых сведений ФНС (ст. 6 129-ФЗ)",
        "основа карточки — открытые данные ФНС согласно ст. 6 129-ФЗ",
    ),
    "fssp_captcha": (
        "капчу всегда решает человек; авто-решение капчи запрещено политикой",
        "капча — только человек; автоматическое решение капчи политикой запрещено",
        "политика запрещает авто-решение капчи: капчу проходит человек",
    ),
    "fees_basis": (
        "расчёт по действующему тарифу; итоговую сумму проверяет человек",
        "сумма посчитана по текущему тарифу; окончательную сумму подтверждает человек",
        "база расчёта — актуальный тариф; финальную цифру утверждает человек",
    ),
    "deadline_basis": (
        "сроки исчисляются по процессуальному кодексу; календарные проверки — на человеке",
        "расчёт срока идёт по нормам кодекса; финальную календарную сверку делает человек",
        "процессуальный срок считается по кодексу; календарь итогово проверяет человек",
    ),
    "npa_source": (
        "тексты норм — из официальных публикаций; перепроверка остаётся за юристом",
        "нормы взяты из официальных источников публикации; финальную сверку делает юрист",
        "основа текстов норм — официальные публикации; за перепроверкой остаётся юрист",
    ),
}


def _seed(version: str) -> bytes:
    s = (os.environ.get(_ENV_SEED) or "").strip()
    return (s or f"public|{version}").encode("utf-8")


def variant(key: str, version: str, *, default_idx: int | None = None) -> int:
    """Индекс варианта для (key, seed, version). Детерминированно, без сети."""
    table = STYLE_VARIANTS.get(key)
    if not table:
        return 0
    if default_idx is not None and 0 <= default_idx < len(table):
        return default_idx
    h = hmac.new(_seed(version), key.encode("utf-8"), hashlib.sha256).digest()
    return h[0] % len(table)


def render_line(key: str, version: str, mapping: dict | None = None) -> str:
    tpl = STYLE_VARIANTS[key][variant(key, version)]
    if mapping:
        for k, v in mapping.items():
            tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


def variant_seed() -> str:
    """Режим сборки (не раскрывает значение seed)."""
    s = (os.environ.get(_ENV_SEED) or "").strip()
    if not s:
        return "public"
    return "client:" + hashlib.sha256(s.encode()).hexdigest()[:8]


def build_flags(version: str) -> list[str]:
    """HMAC-канарейки сборки; без секрета — пустой список (не выдумываем значения)."""
    secret = (os.environ.get(_ENV_SECRET) or "").strip()
    if not secret:
        return []
    out = []
    for i in range(3):
        d = hmac.new(secret.encode(), f"{version}|{i}".encode(), hashlib.sha256).hexdigest()
        out.append(d[:16])
    return out


# ---- §13 интерфейс ---------------------------------------------------------
LICENSE = "AGPL-3.0-only"


def source_url() -> str | None:
    return (os.environ.get("SUD_SOURCE_URL") or "").strip() or None


def source_info(version: str) -> dict:
    url = source_url()
    return {
        "license": LICENSE,
        "source_url": url,
        "how_to_obtain": (f"git clone {url}" if url else
                          "источник этой сборки: репозиторий, указанный установщиком; "
                          "локально — pip download sud-cli --no-deps --no-binary :all:"),
        "agpl_section_13": "каждый пользователь сетевого сервиса на этой программе имеет право "
                           "получить её полный исходный код этой версии",
        "build_mode": variant_seed(),
        "version": version,
    }
