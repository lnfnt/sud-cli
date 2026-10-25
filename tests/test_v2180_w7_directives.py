"""v0.1.1 W7: изменения по юридической проверке 2026-10-25 — ЕФРСБ manual-контур, правила 1.5.0, корпус Пленумов."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sud_cli import efrsb_manual as em
from sud_cli import plenum_corpus as pc

RULES = Path(__file__).resolve().parent.parent / "sud_cli" / "legal_rules.json"
SEED = Path(__file__).resolve().parent.parent / "sud_cli" / "plenums_seed.json"


# --- раздел 1: ЕФРСБ human-in-the-loop (акт §1.2) ---

def test_efrsb_link_plan_ul():
    plan = em.link_plan("ul", inn="7704361225")
    assert plan["base_url"].startswith("https://bankrot.fedresurs.ru")
    assert plan["query"] == "7704361225"
    assert any("человек" in s for s in plan["steps"])
    assert plan["automation"].startswith("off")
    assert "kad" in plan["primary_instead"].lower() or "КАД" in plan["primary_instead"]


def test_efrsb_link_requires_identity():
    with pytest.raises(em.EfrsbManualError):
        em.link_plan("ul")
    with pytest.raises(em.EfrsbManualError):
        em.link_plan("fl")  # без name
    with pytest.raises(em.EfrsbManualError):
        em.link_plan("zzz", inn="1")


def test_efrsb_record_payload_provenance():
    rec = em.record_payload(subject="7704361225", result="none", checked_by="tester",
                            source_ref="efrsb/debtor/123")
    assert "method=efrsb_human_verified" in rec["note"]
    assert "source_ref=efrsb/debtor/123" in rec["note"]
    assert rec["result"] == "none"
    with pytest.raises(em.EfrsbManualError):
        em.record_payload(subject="x", result="bogus", checked_by="tester")
    with pytest.raises(em.EfrsbManualError):
        em.record_payload(subject="x", result="found", checked_by="")


def test_cli_efrsb_link_offline(tmp_path):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "sud_cli", "--json",
                        "efrsb", "link", "--kind", "ul", "--inn", "7704361225"],
                       capture_output=True, text=True, timeout=60)
    out = json.loads(r.stdout)
    assert out["status"] == "ok"
    assert out["data"]["query"] == "7704361225"


# --- раздел 2: legal_rules 1.5.0 (ст.202-204 ГК + гл.III.1 127-ФЗ) ---

def _rules():
    return json.loads(RULES.read_text(encoding="utf-8"))


def test_rules_v150_limitation_audit_shape():
    d = _rules()
    assert d["version"] == "1.5.0"
    la = d["limitation_audit_rules"]
    gk = la["gk_rf_suspension_interruption"]
    assert len(gk["article_202"]["grounds"]) == 5
    assert gk["article_202"]["grounds"][0]["post_suspension_extension_months"] == 6
    assert gk["article_203"]["effect"] == "interruption"
    assert len(gk["article_204"]["rules"]) == 3


def test_rules_v150_avoidance_periods():
    av = _rules()["limitation_audit_rules"]["bankruptcy_avoidance_rules_127_fz"]
    assert av["article_61_2_suspicious"]["item_1_unequal_consideration"]["suspect_period_years"] == 1
    assert av["article_61_2_suspicious"]["item_2_harm_to_creditors"]["suspect_period_years"] == 3
    assert av["article_61_3_preference"]["item_1_preference_general"]["suspect_period_months"] == 1
    assert av["article_61_3_preference"]["item_2_preference_with_knowledge"]["suspect_period_months"] == 6
    assert av["article_61_9_limitation"]["limitation_period_years"] == 1


def test_rules_w7_source_is_act_file():
    assert "VERIFIED" in _rules()["w7_norms_source"]


# --- раздел 3: корпус S-55 ---

def test_plenum_seed_card_validates():
    cards = json.loads(SEED.read_text(encoding="utf-8"))["cards"]
    assert len(cards) == 1
    c = cards[0]
    assert c["doc_id"] == "PP_VS_RF_2015_43"
    assert pc.validate_card(c) == []


def test_plenum_seed_keeps_act_hash():
    c = json.loads(SEED.read_text(encoding="utf-8"))["cards"][0]
    p = c["paragraphs"][0]
    assert p["sha256"] == hashlib.sha256(p["verbatim_text"].encode("utf-8")).hexdigest()
    assert p["act_recorded_sha256"]  # хэш из акта сохранён честно


def test_plenum_import_rejects_bad_hash_without_rehash(tmp_path):
    card = json.loads(SEED.read_text(encoding="utf-8"))["cards"][0]
    bad = json.loads(json.dumps(card))
    bad["doc_id"] = "PP_TEST_BAD"
    bad["paragraphs"][0]["sha256"] = "0" * 64
    with pytest.raises(pc.PlenumError):
        pc.import_card(bad, rehash=False)


def test_plenum_search_and_verify(tmp_path):
    hits = pc.search(tmp_path, "судебного приказа")
    assert hits and hits[0]["doc_id"] == "PP_VS_RF_2015_43"
    v = pc.verify(tmp_path)
    assert v["ok"] is True and v["cards"] >= 1


def test_plenum_cli_roundtrip(tmp_path):
    import subprocess
    import sys
    base = [sys.executable, "-m", "sud_cli", "--json", "--state-dir", str(tmp_path)]
    r = subprocess.run(base + ["plenum", "list"], capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout)["status"] == "ok"
    r2 = subprocess.run(base + ["plenum", "show", "PP_VS_RF_2015_43"], capture_output=True, text=True, timeout=60)
    assert json.loads(r2.stdout)["data"]["number"] == "43"
