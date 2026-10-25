"""the wave §1 (S-12): цепочка инстанций дела поверх VERIFIED deadline_matrix.

Ключевые контракты, которые здесь проверяются:
- порядок задаётся константой по КЛЮЧАМ матрицы, и в цепочку попадают только
  реально существующие ключи этой юрисдикции (несуществующих не выдумываем);
- cure-ключ (duration=null, вне эскалации) в цепочку не входит;
- ни одной даты «из воздуха»: без якоря — due_at=null + needs_anchor;
- unknown instance = цепочки всех юрисдикций с явной пометкой;
- disclaimer справочный — порядок точек не подаётся как норма.
"""
import json

from sud_cli.chain import (
    CHAIN_ORDER_ARBITRATION,
    CHAIN_ORDER_GENERAL,
    CHAIN_ORDER_KAS,
    build_chain,
    chain_keys_for_jurisdiction,
)
from sud_cli.legal_engine import load_rules
from tests.conftest import run_cli

CURE_KEY = "as_cure_left_without_movement"


def _env(tmp_path):
    return {"SUD_CLI_STATE_DIR": str(tmp_path)}


def _j(out):
    return json.loads(out)


def _mk(tmp_path, number="A113-1/2026", extra=None):
    env = _env(tmp_path)
    args = ["--json", "case", "new", "--number", number, "--court", "АС г. Москвы",
            "--plaintiff", "ООО Альфа", "--defendant", "ИП Бета", "--status", "pending"]
    args += list(extra or [])
    code, out, err = run_cli(args, env=env)
    assert code == 0, err
    return env, _j(out)["data"]["case_id"]


def _anchor(env, case_id, key, event_date):
    return run_cli(["--json", "deadlines", "anchor", case_id, "--key", key,
                    "--event-date", event_date, "--method", "вручение",
                    "--evidence", "уведомление", "--by", "lex"], env=env)


def test_chain_empty_case_all_null_needs_anchor(tmp_path):
    env, case_id = _mk(tmp_path, "A113-2/2026", ["--instance", "apk"])
    code, out, err = run_cli(["--json", "deadlines", "chain", case_id], env=env)
    assert code == 0, err
    data = _j(out)["data"]
    chain = data["chain"]
    assert chain, "цепочка apk не должна быть пустой"
    assert [p["key"] for p in chain] == list(CHAIN_ORDER_ARBITRATION)
    for point in chain:
        assert point["due_at"] is None, f"дата выдумана: {point}"
        assert point["needs_anchor"] is True
        assert point["base_event"] == "MISSING"


def test_chain_appeal_anchor_fills_first_point(tmp_path):
    env, case_id = _mk(tmp_path, "A113-3/2026", ["--instance", "apk"])
    code, out, err = _anchor(env, case_id, "as_appeal_decision", "2026-10-01")
    assert code == 0, err
    due = _j(out)["data"]["due_at"]
    assert due
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    first, second = data["chain"][0], data["chain"][1]
    assert first["key"] == "as_appeal_decision"
    assert first["base_event"] == "anchored"
    assert first["due_at"] == due
    assert first["needs_anchor"] is False
    assert first["state"] == "computed"
    # вторая точка не наследует дату первой: без своего якоря она null
    assert second["due_at"] is None
    assert second["needs_anchor"] is True


def test_chain_blocks_next_names_missing_anchor(tmp_path):
    env, case_id = _mk(tmp_path, "A113-4/2026", ["--instance", "apk"])
    _anchor(env, case_id, "as_appeal_decision", "2026-10-01")
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    blocks = data["chain"][0]["blocks_next"]
    assert blocks, "переход к кассации без якоря обязан быть заблокирован"
    assert any("as_cassation_district" in b and "якорь" in b for b in blocks), blocks
    assert data["chain"][-1]["blocks_next"] == [], "у последней точки блоки дальше нет"


def test_chain_instance_override(tmp_path):
    env, case_id = _mk(tmp_path, "A113-5/2026", ["--instance", "apk"])
    data = _j(run_cli(["--json", "deadlines", "chain", case_id, "--instance", "gpk"],
                      env=env)[1])["data"]
    assert data["instance"] == "gpk"
    assert [p["key"] for p in data["chain"]] == list(CHAIN_ORDER_GENERAL)
    assert all(p["norm"] for p in data["chain"]), "норма берётся из матрицы, не из головы"


def test_chain_instance_from_case_field(tmp_path):
    env, case_id = _mk(tmp_path, "A113-6/2026", ["--instance", "kas"])
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    assert data["instance"] == "kas"
    assert [p["key"] for p in data["chain"]] == list(CHAIN_ORDER_KAS)


def test_chain_unknown_instance_lists_all_jurisdictions(tmp_path):
    env, case_id = _mk(tmp_path, "A113-7/2026")  # instance не задана
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    assert data["instance"] == "unknown"
    assert data["chain"] == []
    listed = {c["instance"] for c in data["chains_by_jurisdiction"]}
    assert listed == {"apk", "gpk", "kas"}, listed
    assert "пометк" in data["note"].lower() or "не задана" in data["note"]
    for entry in data["chains_by_jurisdiction"]:
        assert entry["chain"], entry["instance"]


def test_chain_cure_key_excluded(tmp_path):
    env, case_id = _mk(tmp_path, "A113-8/2026", ["--instance", "apk"])
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    keys = {p["key"] for p in data["chain"]}
    assert CURE_KEY not in keys, "cure вне эскалации инстанций"
    # но сам ключ в матрице есть — значит он здесь осознанно исключён, а не потерян
    matrix_keys = _j(run_cli(["--json", "deadlines", "matrix", case_id,
                              "--instance", "apk"], env=env)[1])["data"]
    assert CURE_KEY in {p["key"] for p in matrix_keys["points"]}


def test_chain_keys_are_real_matrix_keys_only(tmp_path):
    matrix = load_rules()["deadline_matrix"]
    for jurisdiction, order in (("arbitration", CHAIN_ORDER_ARBITRATION),
                                ("general_jurisdiction", CHAIN_ORDER_GENERAL),
                                ("kas", CHAIN_ORDER_KAS)):
        keys = chain_keys_for_jurisdiction(jurisdiction, matrix)
        assert keys == list(order), (jurisdiction, keys)
        for key in keys:
            assert matrix[key]["jurisdiction"] == jurisdiction
            assert matrix[key].get("duration"), f"{key}: нулевой duration в цепочке"
    env, case_id = _mk(tmp_path, "A113-9/2026", ["--instance", "apk"])
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    assert data["missing_from_matrix"] == []
    assert {p["key"] for p in data["chain"]} <= set(matrix), "в цепочке только ключи матрицы"


def test_chain_ignores_stored_null_due_when_basis_is_anchored(tmp_path):
    """Матрица пересчитывает срок по якорю: сохранённый due_at=NULL не обманывает цепочку.

    Точка заведена состоянием computed с базой, но с пустым due_at в строке.
    Цепочка берёт due_at у build_matrix (пересчёт от basis), поэтому точка
    выглядит вычисленной, а blocks_next остаётся пустым — врать оператору
    «нужен якорь» на уже якоренной точке движок не имеет права.
    """
    from datetime import date

    from sud_cli.db import get_connection, init_db, insert_deadline_state

    env, case_id = _mk(tmp_path, "A113-14/2026", ["--instance", "apk"])
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    try:
        insert_deadline_state(
            conn, case_id, "as_cassation_district", due_at=None, state="computed",
            basis={"event_date": date.today().isoformat(), "method": "вручение",
                   "evidence": "probe"})
    finally:
        conn.close()
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    second = data["chain"][1]
    assert second["needs_anchor"] is False
    assert second["base_event"] == "anchored"
    assert second["due_at"], "срок пересчитывается от якоря, а не берётся из NULL-строки"
    assert second["state"] == "computed"
    assert data["chain"][0]["blocks_next"] == [], data["chain"][0]["blocks_next"]


def test_point_blockers_distinguishes_uncomputed_due():
    """Юнит чистого хелпера: «нет due_at при базе» != «нужен якорь».

    Через матрицу этот путь сейчас не достижим (duration=null точки вне
    цепочки), поэтому проверяется сама функция, а не выдуманная команда CLI.
    """
    from sud_cli.chain import _point_blockers

    blocks = _point_blockers({"key": "as_cassation_district", "needs_anchor": False,
                              "state": "computed", "due_at": None})
    assert blocks == ["as_cassation_district: due_at не вычислен движком при наличии базы"]
    assert not any("якорь" in b for b in blocks)
    assert _point_blockers(None) == []


def test_chain_disclaimer_is_reference_only(tmp_path):
    env, case_id = _mk(tmp_path, "A113-10/2026", ["--instance", "apk"])
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    assert "справ" in data["disclaimer"].lower()
    assert "норм" in data["disclaimer"]


def test_chain_off_chain_keys_are_listed_not_dropped(tmp_path):
    """Всё, что осталось вне цепочки, видно с причиной: «только ключи цепочки»
    не должно читаться как «остальное потеряно»."""
    matrix = load_rules()["deadline_matrix"]
    env, case_id = _mk(tmp_path, "A113-13/2026", ["--instance", "apk"])
    data = _j(run_cli(["--json", "deadlines", "chain", case_id], env=env)[1])["data"]
    chain_keys = {p["key"] for p in data["chain"]}
    off = {o["key"]: o for o in data["off_chain_keys"]}
    arbitration_keys = {k for k, v in matrix.items() if v.get("jurisdiction") == "arbitration"}
    assert set(off) == arbitration_keys - chain_keys, sorted(off)
    for key, entry in off.items():
        assert entry["reason"], key
        assert entry["norm"] == matrix[key]["legal_basis"], key
    assert CURE_KEY in off


def test_chain_unknown_case_errors(tmp_path):
    env, _ = _mk(tmp_path, "A113-11/2026")
    code, out, err = run_cli(["--json", "deadlines", "chain", "case-no-such"], env=env)
    assert code != 0
    assert "INVALID_ARGUMENTS" in (err + out)


def test_chain_build_matrix_reuse_no_second_anchoring(tmp_path):
    """Цепочка переиспользует build_matrix: состояние точки совпадает с матрицей."""
    env, case_id = _mk(tmp_path, "A113-12/2026", ["--instance", "apk"])
    _anchor(env, case_id, "as_cassation_district", "2026-09-15")
    matrix = _j(run_cli(["--json", "deadlines", "matrix", case_id], env=env)[1])["data"]
    by_key = {p["key"]: p for p in matrix["points"]}
    data = build_chain(tmp_path, case_id)
    for point in data["chain"]:
        src = by_key[point["key"]]
        assert point["due_at"] == src["due_at"]
        assert point["base_event"] == src["base_event"]
        assert point["state"] == src["state"]
