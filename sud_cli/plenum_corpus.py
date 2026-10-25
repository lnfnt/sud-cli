"""Корпус разъяснений высших судов (S-55, акт 25.10 §3).

Карточка судебного акта жёстко структурирована, цитаты несут sha256 фрагмента.
Наполнение — только нормативно-выверенными цитатами официальных публикаций
ВС РФ / ВАС РФ (источники: vsrf.ru/documents/, pravo.gov.ru, «Бюллетень ВС РФ»,
ФКЗ № 8-ФКЗ о сохраняющей силе актов ВАС РФ).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

SEED_FILE = Path(__file__).with_name("plenums_seed.json")
CARD_KEYS = ("doc_id", "court", "instance", "doc_type", "number", "date", "title",
             "official_sources", "status", "paragraphs")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Z0-9_]+$")


class PlenumError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_card(card: dict) -> list[str]:
    """Вернуть список нарушений (пусто = карточка валидна)."""
    errs: list[str] = []
    for k in CARD_KEYS:
        if k not in card:
            errs.append(f"нет поля {k}")
    if not errs:
        if not _ID_RE.match(str(card["doc_id"])):
            errs.append("doc_id должен быть CAPS_UNDERSCORE")
        if not _DATE_RE.match(str(card["date"])):
            errs.append("date в формате YYYY-MM-DD")
        if not isinstance(card["official_sources"], list) or not card["official_sources"]:
            errs.append("official_sources: непустой список")
        if not isinstance(card["paragraphs"], list) or not card["paragraphs"]:
            errs.append("paragraphs: непустой список")
    for p in card.get("paragraphs", []):
        if "verbatim_text" not in p or "sha256" not in p:
            errs.append("в параграфе нужны verbatim_text+sha256")
        elif sha256_text(p["verbatim_text"]) != p["sha256"]:
            errs.append(f"параграф {p.get('paragraph_no')}: sha256 не сходится")
    return errs


def import_card(card: dict, *, rehash: bool = False) -> dict:
    """Импортировать карточку. rehash=True — пересчитать sha256 (при расхождении
    с источником-актом; исходный хэш сохраняется как act_recorded_sha256)."""
    errs = validate_card(card)
    if errs and rehash:
        only_hash_mismatch = all("sha256" in e for e in errs)
        if only_hash_mismatch:
            for p in card["paragraphs"]:
                p["act_recorded_sha256"] = p["sha256"]
                p["sha256"] = sha256_text(p["verbatim_text"])
                p["hash_note"] = "пересчитано при импорте 25.10; хэш акта сохранён"
            errs = validate_card(card)
    if errs:
        raise PlenumError("; ".join(errs))
    return card


def store_path(state_dir: Path) -> Path:
    return Path(state_dir) / "plenums.json"


def _load(state_dir: Path) -> list[dict]:
    sp = store_path(state_dir)
    if sp.exists():
        return json.loads(sp.read_text(encoding="utf-8")).get("cards", [])
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8")).get("cards", [])
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"cards": seed}, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(sp.read_text(encoding="utf-8")).get("cards", [])


def list_cards(state_dir: Path) -> list[dict]:
    out = []
    for c in _load(state_dir):
        out.append({"doc_id": c["doc_id"], "date": c["date"], "number": c["number"],
                    "title": c["title"], "status": c["status"],
                    "instance": c.get("instance", ""), "doc_type": c.get("doc_type", "")})
    return out


def get_card(state_dir: Path, doc_id: str) -> dict:
    for c in _load(state_dir):
        if c["doc_id"] == doc_id:
            return c
    raise PlenumError(f"карточка {doc_id} не найдена")


def search(state_dir: Path, text: str) -> list[dict]:
    needle = (text or "").lower()
    hits = []
    for c in _load(state_dir):
        for p in c.get("paragraphs", []):
            blob = " ".join([str(p.get("key_topic", "")), p.get("verbatim_text", ""),
                             c.get("title", "")]).lower()
            if needle and needle in blob:
                hits.append({"doc_id": c["doc_id"], "paragraph_no": p.get("paragraph_no"),
                             "key_topic": p.get("key_topic", ""),
                             "norm_links": p.get("norm_links", []),
                             "excerpt": p["verbatim_text"][:240]})
    return hits


def verify(state_dir: Path) -> dict:
    cards = _load(state_dir)
    bad = []
    for c in cards:
        for p in c.get("paragraphs", []):
            if sha256_text(p.get("verbatim_text", "")) != p.get("sha256"):
                bad.append(f"{c['doc_id']}#{p.get('paragraph_no')}")
    return {"cards": len(cards), "ok": not bad, "broken": bad}


def add_card(state_dir: Path, card: dict, *, rehash: bool = True) -> dict:
    card = import_card(card, rehash=rehash)
    sp = store_path(state_dir)
    cards = _load(state_dir)
    if any(c["doc_id"] == card["doc_id"] for c in cards):
        raise PlenumError(f"карточка {card['doc_id']} уже в корпусе")
    cards.append(card)
    sp.write_text(json.dumps({"cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"doc_id": card["doc_id"], "paragraphs": len(card["paragraphs"])}
