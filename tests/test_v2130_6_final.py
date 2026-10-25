"""the wave §6: финал волны — версия 0.1.1, CHANGELOG/README, bus-free и гигиена.

Здесь проверяется не предметная логика (её закрыли test_v2130_1..5), а границы
волны: схема не bump'ится, шина/внешние процессы не дёргаются, нормативные
строки не хардкодятся, CJK в файлы волны не попал, счётчик новых тестов
соответствует плану ТЗ (>=45).
"""
import inspect
import json
import os
import re
import sqlite3
from pathlib import Path

import sud_cli
from sud_cli.db import SCHEMA_VERSION, get_case, get_connection
from tests.conftest import run_cli

ROOT = Path(__file__).resolve().parents[1]

W6B2_MODULE_FILES = [
    "sud_cli/chain.py", "sud_cli/conflicts.py", "sud_cli/reports.py",
    "sud_cli/backup.py", "sud_cli/metrics.py", "sud_cli/pdn.py",
    "sud_cli/db.py", "sud_cli/cli.py", "sud_cli/mcp_server.py",
]
W6B2_TEST_FILES = [
    "tests/test_v2130_1_chain.py", "tests/test_v2130_2_reps.py",
    "tests/test_v2130_3_conflicts.py", "tests/test_v2130_4_reports.py",
    "tests/test_v2130_5_backup_metrics_pdn.py", "tests/test_v2130_6_final.py",
]
WAVE_FILES = W6B2_MODULE_FILES + W6B2_TEST_FILES
# тот же класс символов, что в проверенном гейте v2120 (escape-форма, без
# литеральных иероглифов в исходнике)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                    r"\u3000-\u303f\uff00-\uffef]")

#: Модули волны, которым запрещено что-либо запускать и куда-либо писать за
#: пределы state-dir: bus-free контракт (эталон — reminders/court_alerts).
BUS_FREE_MODULES = ("sud_cli.chain", "sud_cli.conflicts", "sud_cli.reports",
                    "sud_cli.backup", "sud_cli.metrics", "sud_cli.pdn")


def test_version_2_13_0_pair():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert sud_cli.__version__ == "0.1.1"
    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert m and m.group(1) == sud_cli.__version__


def test_cli_version_output_matches():
    code, out, err = run_cli(["--version"])
    assert code == 0 and "0.1.1" in (out + err)


def test_changelog_has_2130_and_readme_sections():
    ch = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.1.1" in ch.split("## 2.12.0")[0]
    rm = (ROOT / "README.md").read_text(encoding="utf-8")
    for section in ("Chain", "Reps", "Conflicts", "Reports", "Backup",
                    "Metrics", "Purge"):
        assert section.lower() in rm.lower(), section


def test_readme_documents_real_commands():
    rm = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in ("deadlines chain", "case rep add", "case rep list",
                    "calendar conflicts", "report portfolio", "report case",
                    "backup create", "backup verify", "backup restore",
                    "case export", "case purge", "metrics"):
        assert command in rm, command


def test_wave_files_have_no_cjk():
    for rel in WAVE_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        hits = CJK_RE.findall(text)
        assert not hits, f"{rel}: CJK-символы {sorted(set(hits))}"


def test_schema_version_not_bumped_by_wave(tmp_path):
    """the wave добавил таблицу и колонку ensure-паттерном: версия схемы = '3'."""
    assert SCHEMA_VERSION == "3"
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", "A118-1/2026",
                              "--court", "АС г. Москвы", "--plaintiff", "P",
                              "--defendant", "D", "--status", "new",
                              "--instance", "apk"], env=env)
    assert code == 0, err
    code, out, err = run_cli(["--json", "doctor"], env=env)
    assert code == 0, err
    assert json.loads(out)["data"]["schema_version"] == "3"
    conn = sqlite3.connect(str(tmp_path / "store.db"))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        case_cols = [r[1] for r in conn.execute("PRAGMA table_info(cases)")]
    finally:
        conn.close()
    assert "case_representatives" in tables
    assert "instance" in case_cols


#: Что считается «вызвать внешний мир»: импорт процесса/сети/шины или вызов.
#: Проза в docstring («шину a2a не трогаем») гейт не ловит — ловится именно
#: исполнимая форма, иначе комментарий о запрете читался бы как нарушение.
FORBIDDEN_IMPORT_ROOTS = ("subprocess", "socket", "urllib", "http", "requests",
                          "smtplib", "ftplib", "multiprocessing", "threading",
                          "asyncio", "shlex")
FORBIDDEN_CALLEES = ("system", "popen", "execv", "execve", "spawn", "run",
                     "Popen", "check_output", "call")


def _forbidden_forms(src):
    """Импорты внешних библиотек и вызовы os.system/Popen из исходника."""
    import ast

    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_IMPORT_ROOTS or root in ("a2a", "docx"):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_IMPORT_ROOTS or root in ("a2a", "docx"):
                found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in FORBIDDEN_CALLEES:
                holder = getattr(func, "value", None)
                holder_name = getattr(holder, "id", "") if holder is not None else ""
                if holder_name in ("os", "subprocess") or name in ("Popen",):
                    found.append(f"{holder_name}.{name}()" if holder_name else f"{name}()")
    return found


def test_bus_free_wave_modules_never_spawn_processes():
    for module_name in BUS_FREE_MODULES:
        module = __import__(module_name, fromlist=["__file__"])
        offenders = _forbidden_forms(inspect.getsource(module))
        assert not offenders, f"{module_name}: {offenders}"


def test_bus_free_cli_handlers_never_spawn_processes():
    from sud_cli import cli

    for handler in ("cmd_deadlines_chain", "cmd_calendar_conflicts",
                    "cmd_report_portfolio", "cmd_report_case", "cmd_metrics",
                    "cmd_backup_create", "cmd_backup_verify", "cmd_backup_restore",
                    "cmd_case_export", "cmd_case_purge", "cmd_case_rep_add",
                    "cmd_case_rep_list"):
        offenders = _forbidden_forms(inspect.getsource(getattr(cli, handler)))
        assert not offenders, f"{handler}: {offenders}"


def test_pdn_text_not_hardcoded_in_source():
    """Нормативные строки ПДн читаются из правил, а не вшиты в модуль."""
    from sud_cli import pdn

    src = inspect.getsource(pdn)
    policy = json.loads((ROOT / "sud_cli" / "legal_rules.json").read_text(
        encoding="utf-8"))["pdn_policy"]
    for needle in ("152-ФЗ", "229-ФЗ", "63-ФЗ"):
        assert needle not in src, f"pdn.py содержит «{needle}» в обход правил"
    # проверка на живом значении: строка правил не должна встречаться в коде
    assert policy["retention"] not in src
    for ground in policy["grounds"]:
        assert ground not in src


def test_wave_commands_spawn_nothing_at_runtime(tmp_path, monkeypatch):
    """Bus-free не только по исходнику: под блокировкой Popen команды проходят.

    Статический гейт ловит импорт, но не вызов, собранный динамически. Здесь
    subprocess.Popen/run/check_output и os.system/os.spawnv взрываются, и все
    команды волны обязаны отработать чисто — иначе «bus-free» остаётся
    обещанием в комментарии.
    """
    import subprocess

    def _boom(*a, **kw):
        raise AssertionError(f"попытка запустить внешний процесс: {a[:2]}")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "spawnv", _boom)

    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", "A118-3/2026",
                              "--court", "АС", "--plaintiff", "P", "--defendant", "D",
                              "--status", "pending", "--instance", "apk"], env=env)
    assert code == 0, err
    cid = json.loads(out)["data"]["case_id"]
    code, out, err = run_cli(["--json", "case", "rep", "add", cid, "--side", "plaintiff",
                              "--name", "Иванов"], env=env)
    assert code == 0, err
    for argv in (["--json", "deadlines", "chain", cid],
                 ["--json", "calendar", "conflicts"],
                 ["--json", "report", "portfolio"],
                 ["--json", "report", "case", cid],
                 ["--json", "metrics"],
                 ["--json", "backup", "create"],
                 ["--json", "case", "export", cid],
                 ["--json", "case", "export", cid, "--format", "md"]):
        code, out, err = run_cli(argv, env=env)
        assert code == 0, f"{argv}: {err}"

    # деструктив без --yes: отказ (код != 0) и ничего не удалено — тоже без
    # внешних процессов
    code, out, err = run_cli(["--json", "case", "purge", cid], env=env)
    assert code != 0
    assert json.loads(err)["data"]["status"] == "needs_confirmation"
    conn = get_connection(tmp_path)
    try:
        assert get_case(conn, cid) is not None
    finally:
        conn.close()

    code, out, err = run_cli(["--json", "case", "purge", cid, "--yes"], env=env)
    assert code == 0, err


def test_rules_files_untouched_by_wave():
    """legal_rules/jurisdiction_rules — финал v1.4.0: волна их не меняла."""
    rules = json.loads((ROOT / "sud_cli" / "legal_rules.json").read_text(
        encoding="utf-8"))
    assert rules["version"] == "1.5.0"
    assert "pdn_policy" in rules and "deadline_matrix" in rules
    matrix = rules["deadline_matrix"]
    # цепочка опирается на существующие ключи с ненулевым duration
    from sud_cli.chain import CHAIN_ORDER_BY_JURISDICTION

    for jurisdiction, keys in CHAIN_ORDER_BY_JURISDICTION.items():
        assert keys, jurisdiction
        for key in keys:
            assert key in matrix, (jurisdiction, key)
            assert matrix[key]["jurisdiction"] == jurisdiction, key
            assert matrix[key].get("duration") is not None, key
    # cure-ключ нигде в цепочках
    assert "as_cure_left_without_movement" not in sum(
        CHAIN_ORDER_BY_JURISDICTION.values(), ())


def test_v2130_suite_has_at_least_45_tests():
    collected = 0
    for rel in W6B2_TEST_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        collected += len(re.findall(r"^def test_", text, re.M))
    assert collected >= 45, f"новых тестов {collected}, план ТЗ >=45"


def test_new_cli_commands_are_discoverable(tmp_path):
    """Каждая команда волны реально существует в дереве argparse."""
    from sud_cli.cli import build_parser

    parser = build_parser()
    for argv in (["deadlines", "chain", "case-x"], ["case", "rep", "list", "case-x"],
                 ["calendar", "conflicts"], ["report", "portfolio"],
                 ["report", "case", "case-x"], ["backup", "verify", "/tmp/x.db"],
                 ["metrics"], ["case", "export", "case-x"],
                 ["case", "purge", "case-x"]):
        args = parser.parse_args(argv)
        assert getattr(args, "func", None) is not None, argv


def test_wave_writes_nothing_outside_state_dir(tmp_path):
    """backup create с дефолтным путём остаётся внутри state-dir; audit — тоже."""
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", "A118-2/2026",
                              "--court", "АС", "--plaintiff", "P", "--defendant", "D",
                              "--status", "new"], env=env)
    assert code == 0, err
    cid = json.loads(out)["data"]["case_id"]
    code, out, err = run_cli(["--json", "backup", "create"], env=env)
    assert code == 0, err
    path = json.loads(out)["data"]["path"]
    assert path.startswith(str(tmp_path)), path
    code, out, err = run_cli(["--json", "case", "purge", cid, "--yes"], env=env)
    assert code == 0, err
    assert (tmp_path / "deadline_audit.jsonl").is_file()
    assert list(tmp_path.glob("*.jsonl")) == [tmp_path / "deadline_audit.jsonl"]
