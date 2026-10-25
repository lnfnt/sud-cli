"""Health-check / ``case-search doctor`` diagnostics.

All checks return JSON-serializable items with ``id``, ``label``,
``status`` (ok/warn/fail) and ``detail``.  Nothing here prints license
keys or other secrets.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Iterable

from . import storage as _storage

# Known optional dependencies the operator may care about even when the
# current source tree does not import them directly.
_OPTIONAL_DEPS = frozenset({"httpx", "requests", "pytesseract"})

# Network targets for the connectivity smoke test.
_NETWORK_TARGETS = (("kad.arbitr.ru", 443), ("vsrf.ru", 443))


class _Status:
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


def _worsen(current: str, incoming: str) -> str:
    order = {_Status.OK: 0, _Status.WARN: 1, _Status.FAIL: 2, "warn": 1, "fail": 2, "ok": 0, "timeout": 1, "blocked": 1}
    return current if order.get(current, 1) >= order.get(incoming, 1) else incoming


def _repo_root() -> Path:
    """Repository root, used to locate browser-runtime without walking up."""
    return Path(__file__).resolve().parent.parent


def _is_stdlib(module_name: str) -> bool:
    top = module_name.split(".")[0]
    if top in ("case_search",):
        return True
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return top in names
    # Fallback for unusual interpreters: a small safe deny-list.
    return top in {"os", "sys", "json", "re", "pathlib", "datetime",
                   "time", "hashlib", "base64", "hmac", "typing"}


def _imported_third_party_modules() -> set[str]:
    """Scan the package source for third-party imports (lazy ones too).

    Returns top-level module names actually referenced by ``case_search``,
    e.g. ``{"pymupdf"}``.  This lets the doctor decide whether a missing
    ``httpx``/``requests`` is a failure or merely informational.
    """
    pkg_dir = Path(__file__).resolve().parent
    imported: set[str] = set()
    for path in pkg_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import
                    continue
                if node.module:
                    imported.add(node.module.split(".")[0])
    return {m for m in imported if not _is_stdlib(m)}


def _check_item(check_id: str, label: str, status: str, detail: str,
                *, critical: bool = False) -> dict:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "critical": critical,
    }


def _check_python_version() -> dict:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        return _check_item("python-version", "Python version >= 3.11",
                           _Status.OK, version, critical=True)
    return _check_item("python-version", "Python version >= 3.11",
                       _Status.FAIL, version, critical=True)


def _check_package() -> dict:
    try:
        import case_search
        version = getattr(case_search, "__version__", "unknown")
        return _check_item("package-import", "case_search importable",
                           _Status.OK, f"version {version}", critical=True)
    except Exception as exc:  # pragma: no cover
        return _check_item("package-import", "case_search importable",
                           _Status.FAIL, str(exc), critical=True)


def _check_dependencies() -> dict:
    imported = _imported_third_party_modules()
    known = {"pymupdf", "httpx", "requests", "pytesseract"}
    required = imported & known
    statuses: list[str] = []
    details: list[str] = []
    worst = _Status.OK

    for name in sorted(known):
        is_required = name in required
        try:
            importlib.import_module(name)
            present = True
        except Exception:
            present = False

        if present:
            details.append(f"{name}: present")
            statuses.append(_Status.OK)
        elif is_required:
            details.append(f"{name}: missing (required by source)")
            worst = _worsen(worst, _Status.FAIL)
            statuses.append(_Status.FAIL)
        else:
            details.append(f"{name}: missing (optional)")
            worst = _worsen(worst, _Status.WARN)
            statuses.append(_Status.WARN)

    return _check_item("dependencies", "Dependencies importable",
                       worst, "; ".join(details),
                       critical=bool(required) or _Status.FAIL in statuses)


def _check_tesseract() -> dict:
    binary = shutil.which("tesseract")
    if binary:
        return _check_item("tesseract-binary", "Tesseract binary on PATH",
                           _Status.OK, binary)
    return _check_item("tesseract-binary", "Tesseract binary on PATH",
                       _Status.WARN, "not found on PATH")


def _check_browser_runtime() -> dict:
    """Check configured/local browser runtime directories, no mutation."""
    from . import config as _cfg

    candidates: list[Path] = []
    camofox_url = _cfg.get("camofox_url")
    if camofox_url and "://" not in camofox_url:
        candidates.append(Path(camofox_url))
    # Default on-disk browser runtime shipped with the repo.
    candidates.append(_repo_root() / "browser-runtime")

    found: list[str] = []
    missing: list[str] = []
    for path in candidates:
        if path.exists():
            found.append(str(path))
        else:
            missing.append(str(path))

    if found:
        detail = f"found: {', '.join(found)}"
        if missing:
            detail += f"; missing: {', '.join(missing)}"
        return _check_item("browser-runtime", "Camofox/browser runtime",
                           _Status.OK, detail)
    detail = f"none found; checked: {', '.join(missing)}"
    return _check_item("browser-runtime", "Camofox/browser runtime",
                       _Status.WARN, detail)


def _check_sudact_runtime() -> dict:
    """Check the JS browser runtime used by sudact search/case-history."""
    from .sudact import runtime as _rt

    result = _rt.runtime_status(timeout=5.0)
    endpoint = result.get("endpoint", _rt.DEFAULT_BASE_URL + "/health")
    status = result.get("status")
    browser_connected = result.get("browserConnected")
    pool_size = result.get("poolSize")

    health_bits: list[str] = []
    if browser_connected is not None:
        health_bits.append(f"browserConnected={browser_connected}")
    if pool_size is not None:
        health_bits.append(f"poolSize={pool_size}")
    health_txt = f" ({'; '.join(health_bits)})" if health_bits else ""

    if result["ok"] and browser_connected is True:
        detail = f"up and ready at {endpoint}{health_txt}"
        if status:
            detail += f" (HTTP {status})"
        return _check_item("sudact-runtime",
                           "Browser runtime for sudact",
                           _Status.OK, detail)

    if result["ok"]:
        detail = f"up but browser dead at {endpoint}{health_txt}"
    else:
        detail = f"not reachable at {endpoint}"
        if status:
            detail += f" (HTTP {status})"
        detail += f": {result.get('error', 'unknown')}"
    return _check_item("sudact-runtime",
                       "Browser runtime for sudact",
                       _Status.WARN, detail)


def _tcp_check(host: str, port: int, timeout: float = 5.0) -> tuple[str, str]:
    """Return (status, detail) for a single TCP connect probe."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return _Status.OK, f"{host}:{port} reachable"
    except socket.timeout:
        return "timeout", f"{host}:{port} timed out"
    except OSError as exc:
        return "blocked", f"{host}:{port} blocked ({exc.__class__.__name__})"


def _check_network(targets: Iterable[tuple[str, int]] | None = None) -> dict:
    targets = targets or _NETWORK_TARGETS
    results: list[str] = []
    worst = _Status.OK
    for host, port in targets:
        status, detail = _tcp_check(host, port)
        results.append(detail)
        worst = _worsen(worst, status)
    return _check_item("network-reachability",
                       "Network reachability (court hosts)",
                       worst, "; ".join(results))


def _human_size(num_bytes: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PiB"


def _check_storage() -> dict:
    root = _storage.default_storage_root().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        return _check_item("storage", "Default data dir writable",
                           _Status.FAIL, f"{root} not writable: {exc}",
                           critical=True)

    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = None

    free_detail = f"free {_human_size(free)}" if free is not None else "free space unknown"
    return _check_item("storage", "Default data dir writable",
                       _Status.OK, f"{root} writable, {free_detail}",
                       critical=True)


def _check_license() -> dict:
    from .license import load_license, LicenseError

    try:
        payload = load_license()
        expires = payload.get("expires")
        if expires:
            detail = f"valid until {time.strftime('%Y-%m-%d', time.gmtime(expires))}"
        else:
            detail = "valid (no expiry)"
        return _check_item("license", "License state",
                           _Status.OK, detail, critical=True)
    except LicenseError as exc:
        msg = str(exc).lower()
        if "expired" in msg:
            return _check_item("license", "License state",
                               _Status.FAIL, "expired", critical=True)
        if "no license key" in msg:
            return _check_item("license", "License state",
                               _Status.WARN, "missing", critical=False)
        return _check_item("license", "License state",
                           _Status.FAIL, "invalid", critical=True)


def run_doctor(*, verbose: bool = False) -> dict:
    """Run all health checks and return a JSON-serializable report."""
    checks = [
        _check_python_version(),
        _check_package(),
        _check_dependencies(),
        _check_tesseract(),
        _check_browser_runtime(),
        _check_sudact_runtime(),
        _check_network(),
        _check_storage(),
        _check_license(),
    ]
    failed = any(check["status"] == _Status.FAIL for check in checks)
    report: dict = {
        "ok": not failed,
        "failed": failed,
        "checks": checks,
    }
    if verbose:
        report["verbose"] = True
    return report


def format_table(report: dict) -> str:
    """Human-readable checklist (no secrets)."""
    lines: list[str] = []
    for check in report["checks"]:
        status = check["status"].upper()
        marker = {"OK": "[OK]", "WARN": "[WARN]", "FAIL": "[FAIL]"}.get(
            status, f"[{status}]")
        crit = " *" if check.get("critical") else ""
        lines.append(f"{marker:6} {check['label']}{crit}")
        lines.append(f"         {check['detail']}")
    summary = "PASS" if report.get("ok") else "FAIL"
    lines.append(f"\nhealth: {summary}")
    return "\n".join(lines)


def main_doctor(*, json_out: bool = False, verbose: bool = False) -> int:
    """Entry point used by ``case_search.cli:cmd_doctor``."""
    report = run_doctor(verbose=verbose)
    if json_out:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_table(report))
    return 0 if report["ok"] else 1
