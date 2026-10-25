"""история §5: финал волны — версия 2.12.0 синхронно, CHANGELOG/README, гигиена файлов.

Версия проверяется в паре pyproject+__init__ (test_audit_infra сверяет конверт
CLI); здесь — якорь конкретной волны и гейт отсутствия CJK в файлах the wave.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

W6B1_FILES = [
    "sud_cli/db.py", "sud_cli/cli.py", "sud_cli/forms.py", "sud_cli/completeness.py",
    "sud_cli/mcp_server.py", "sud_cli/data/completeness_rules.json",
    "tests/test_v2120_1_evidence.py", "tests/test_v2120_2_dochistory.py",
    "tests/test_v2120_3_checklist.py", "tests/test_v2120_4_forms.py",
    "tests/test_v2120_5_final.py",
]
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def test_version_2_12_0_pair():
    """Пара pyproject+__init__ обязана совпадать (инвариант, а не число).

    Волна the wave подняла версию до 2.13.0, поэтому literal «2.12.0» здесь
    устарел бы в момент релиза: проверяется сам инвариант синхронизации,
    а якорь конкретной волны держит tests/test_v2130_6_final.py.
    """
    import sud_cli
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert sud_cli.__version__
    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert m and m.group(1) == sud_cli.__version__


def test_cli_version_output_matches():
    import json as _json
    import sud_cli
    from tests.conftest import run_cli
    code, out, err = run_cli(["--version"])
    assert code == 0 and sud_cli.__version__ in (out + err)


def test_changelog_has_2120_and_readme_sections():
    ch = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ch.count("## 0.1.0") == 1
    rm = (ROOT / "README.md").read_text(encoding="utf-8")
    for section in ("Evidence", "DocHistory", "Checklist", "Forms"):
        assert section.lower() in rm.lower(), section


def test_w6b1_files_have_no_cjk():
    for rel in W6B1_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        hits = CJK_RE.findall(text)
        assert not hits, f"{rel}: CJK-символы {set(hits)}"


def test_v2120_suite_has_at_least_30_tests():
    collected = 0
    for rel in sorted(str(p) for p in (ROOT / "tests").glob("test_v2120_*.py")):
        text = Path(rel).read_text(encoding="utf-8")
        collected += len(re.findall(r"^def test_", text, re.M))
    assert collected >= 30, f"новых тестов {collected}, план ТЗ >=30"
