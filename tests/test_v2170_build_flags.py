"""v0.1.1 §13-интерфейс: флаги представления вариантной поверхности + build_flag + §13-интерфейс."""
from __future__ import annotations

import json

from sud_cli import _wm
from sud_cli.cli import main


def _keys():
    return sorted(_wm.STYLE_VARIANTS)


def test_variant_deterministic_same_seed(monkeypatch):
    monkeypatch.setenv("SUD_BUILD_SEED", "client-X")
    a = [_wm.variant(k, "0.1.1") for k in _keys()]
    b = [_wm.variant(k, "0.1.1") for k in _keys()]
    assert a == b


def test_variant_differs_across_seeds(monkeypatch):
    monkeypatch.setenv("SUD_BUILD_SEED", "client-X")
    a = [_wm.variant(k, "0.1.1") for k in _keys()]
    monkeypatch.setenv("SUD_BUILD_SEED", "client-Z")
    b = [_wm.variant(k, "0.1.1") for k in _keys()]
    assert a != b, "разные seed обязаны давать разные векторы выбора"


def test_semantic_invariants_all_variants():
    # каждый вариант ключа обязан содержать тот же смысловой якорь, что и все остальные
    anchors = {
        "status_hint": "verdict=unknown",
        "egrul_basis": "129-ФЗ",
        "fssp_captcha": "капч",
        "fees_basis": "тариф",
        "deadline_basis": "кодекс",
        "npa_source": "официальн",
    }
    for key, anchor in anchors.items():
        table = _wm.STYLE_VARIANTS[key]
        assert len(table) >= 3, key
        for v in table:
            assert anchor in v, f"{key}: якорь {anchor!r} потерян в {v!r}"


def test_build_flags_absent_without_secret_empty_with(monkeypatch):
    monkeypatch.delenv("SUD_BUILD_SECRET", raising=False)
    assert _wm.build_flags("0.1.1") == []
    monkeypatch.setenv("SUD_BUILD_SECRET", "topsecret")
    c = _wm.build_flags("0.1.1")
    assert len(c) == 3
    assert all(len(x) == 16 and all(ch in "0123456789abcdef" for ch in x) for x in c)
    assert c == _wm.build_flags("0.1.1")  # детерминированно


def test_variant_seed_hides_value(monkeypatch):
    monkeypatch.setenv("SUD_BUILD_SEED", "client-SECRET-NAME")
    fp = _wm.variant_seed()
    assert fp.startswith("client:")
    assert "SECRET" not in fp
    monkeypatch.delenv("SUD_BUILD_SEED")
    assert _wm.variant_seed() == "public"


def test_source_info_agpl_section13(monkeypatch):
    monkeypatch.delenv("SUD_BUILD_SEED", raising=False)
    info = _wm.source_info("0.1.1")
    assert info["license"] == "AGPL-3.0-only"
    assert info["source_url"] is None
    assert "pip download sud-cli" in info["how_to_obtain"]
    assert "исходный код" in info["agpl_section_13"]
    monkeypatch.setenv("SUD_SOURCE_URL", "https://github.com/example/sud-cli")
    info2 = _wm.source_info("0.1.1")
    assert info2["source_url"].startswith("https://")
    assert "git clone" in info2["how_to_obtain"]


def test_cli_license_command_json(capsys, monkeypatch):
    monkeypatch.delenv("SUD_BUILD_SEED", raising=False)
    monkeypatch.delenv("SUD_BUILD_SECRET", raising=False)
    rc = main(["--json", "license"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["license"] == "AGPL-3.0-only"
    assert out["data"]["build_mode"] == "public"
    assert out["data"]["build_flags"] == []
    assert out["meta"]["command"] == "license"


def test_practice_status_hint_rendered():
    from sud_cli import practice_status as ps
    assert "verdict=unknown" in ps.HINT
    assert ps.HINT in _wm.STYLE_VARIANTS["status_hint"]
