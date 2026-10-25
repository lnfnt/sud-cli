"""v2.15.0 S-61 ФССП: (Б) ручной контур + (В) официальная статистика. Офлайн-тесты."""
import json
import os
import subprocess

import pytest

from sud_cli import fssp_manual as fm
from sud_cli import fssp_stats as fs


def test_check_plan_fiz():
    plan = fm.check_plan("fiz", name="Иванов Иван", birth_date="01.01.1980", region="77")
    assert plan["url"] == fm.FSSP_BASE_URL
    assert plan["params"]["ФИО"] == "Иванов Иван"
    assert plan["params"]["дата рождения"] == "01.01.1980"
    assert any("капч" in s.lower() for s in plan["checklist"])


def test_check_plan_validation():
    with pytest.raises(ValueError):
        fm.check_plan("fiz")
    with pytest.raises(ValueError):
        fm.check_plan("ur")
    with pytest.raises(ValueError):
        fm.check_plan("bogus", name="x")


def test_record_payload_shapes():
    p = fm.record_payload(subject="ООО Ромашка", result="found",
                          checked_by="tester", notes="4 ИП на 120к")
    assert "Ромашка" in p["title"]
    assert "производства найдены" in p["note"]
    assert "Проверил(а): tester" in p["note"]
    with pytest.raises(ValueError):
        fm.record_payload(subject="x", result="bogus", checked_by="y")
    with pytest.raises(ValueError):
        fm.record_payload(subject="", result="none", checked_by="y")


STATS_FIXTURE = (
    'id;title;count\n'
    '"00";"Центральный аппарат ФССП России";"60213386"\n'
    '"77";"77 Москва";"468683"\n'
    '"78";"78 Санкт-Петербург";"393629"\n'
    '"50";"50 Московская область";"338312"\n'
)


def _fake_http(monkeypatch, payload=STATS_FIXTURE.encode("utf-8")):
    calls = []

    def fake(url, *, timeout=30):
        calls.append(url)
        return payload
    monkeypatch.setattr(fs, "_http_get", fake)
    return calls


def test_stats_parse_and_filter(monkeypatch):
    _fake_http(monkeypatch)
    res = fs.statistics()
    assert res["count"] == 4
    assert res["center_total_hint"] == 60213386
    _fake_http(monkeypatch)
    res77 = fs.statistics("77")
    assert [r["region_id"] for r in res77["rows"]] == ["77"]
    assert res77["rows"][0]["active_executions"] == 468683
    _fake_http(monkeypatch)
    resm = fs.statistics("москв")   # «77 Москва» содержит москв, «Московская» — нет
    assert {r["region_id"] for r in resm["rows"]} == {"77"}
    _fake_http(monkeypatch)
    resob = fs.statistics("область")
    assert {r["region_id"] for r in resob["rows"]} == {"50"}


def test_stats_garbage_fail_closed(monkeypatch):
    calls = _fake_http(monkeypatch, payload=b'{"error":"nope"}')
    with pytest.raises(fs.FsspStatsError):
        fs.statistics()


def test_stats_breaker_cooldown_wrapped(monkeypatch):
    """Лимит подключений ФССП -> breaker cooldown -> FsspStatsError (не crash)."""
    from sud_cli import sud_netguard as lg

    def fake_check(url):
        raise lg.BreakerOpenError("fssp.gov.ru", 42.0, "rate")
    monkeypatch.setattr(lg, "check_before_fetch", fake_check)
    with pytest.raises(fs.FsspStatsError) as ei:
        fs._http_get(fs.STATS_URL)
    assert "cooldown" in str(ei.value)


def test_cli_fssp_check_and_record(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def cli(*args):
        return subprocess.run(["sud-cli", *args], capture_output=True, text=True,
                              env=env, timeout=30)

    r = cli("--json", "fssp", "check", "--kind", "ur", "--inn", "7707083893")
    assert r.returncode == 0
    d = json.loads(r.stdout)
    assert d["status"] == "ok"
    assert d["data"]["url"].startswith("https://fssp.gov.ru/iss/ip")

    st = str(tmp_path / "state")
    r2 = cli("--state-dir", st, "--json", "case", "new", "--number", "А40-1/2026",
             "--court", "АС Москвы", "--plaintiff", "ООО Ромашка", "--defendant", "ИП Крокодил")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    case_id = json.loads(r2.stdout)["data"]["case_id"]

    r3 = cli("--state-dir", st, "--json", "fssp", "record", "--case", case_id,
             "--subject", "ПАО СБЕРБАНК", "--result", "none", "--checked-by", "tester")
    assert r3.returncode == 0, r3.stdout + r3.stderr

    r4 = cli("--state-dir", st, "--json", "case", "evidence", "list", case_id)
    d4 = json.loads(r4.stdout)
    data4 = d4["data"]
    items = data4["items"] if isinstance(data4, dict) else data4
    titles = [i["title"] for i in items]
    assert any("ФССП" in t for t in titles)
