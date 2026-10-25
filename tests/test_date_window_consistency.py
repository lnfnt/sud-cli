"""Consistency test: sudrf date_window default is uniform (5) across CLI, API, live.

Guards against the drift found in audit 2026-09-19 (CLI=5, API=5, live.py=3).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CANONICAL_DEFAULT = 5


def _collect_date_window_defaults() -> dict[str, int]:
    """Scan live.py and public_api.py ASTs for date_window keyword defaults."""
    out: dict[str, int] = {}
    for rel in ("case_search/sudrf/live.py", "case_search/public_api.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.kwonlyargs:
                    if arg.arg == "date_window":
                        default = node.args.kw_defaults[
                            node.args.kwonlyargs.index(arg)]
                        if isinstance(default, ast.Constant):
                            out[f"{rel}:{node.name}"] = default.value
    # CLI parser default
    cli_src = (REPO_ROOT / "case_search/cli.py").read_text(encoding="utf-8")
    for line in cli_src.splitlines():
        if "--date-window" in line and "default=" in line:
            import re
            m = re.search(r"default=(\d+)", line)
            if m:
                out["cli.py:--date-window"] = int(m.group(1))
    return out


class TestDateWindowConsistency:
    def test_all_defaults_are_canonical(self):
        defaults = _collect_date_window_defaults()
        assert defaults, "no date_window defaults found — scan broken"
        bad = {k: v for k, v in defaults.items() if v != CANONICAL_DEFAULT}
        assert not bad, f"date_window drift: {bad}"

    def test_public_api_signatures_documented_default(self):
        from case_search import public_api as pa
        import inspect
        for fn_name in ("get_sudrf_case", "get_sudrf_case_by_vnkod"):
            sig = inspect.signature(getattr(pa, fn_name))
            assert sig.parameters["date_window"].default == CANONICAL_DEFAULT
