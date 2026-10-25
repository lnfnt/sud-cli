"""Tests for ``case_search.doctor``.

All network probes are mocked; no real TCP connections are made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search import doctor  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Every test gets a fake TCP probe so CI never touches the internet."""
    def _fake_probe(host: str, port: int, timeout: float = 0.0) -> tuple[str, str]:
        return (doctor._Status.OK, f"{host}:{port} reachable (mocked)")

    monkeypatch.setattr(doctor, "_tcp_check", _fake_probe)
    # Sudact runtime probe is exercised in its own tests; keep doctor stable.
    monkeypatch.setattr(
        "case_search.sudact.runtime.probe_browser_runtime",
        lambda **_: {"ok": True,
                     "endpoint": "http://127.0.0.1:9377/health",
                     "status": "200", "error": ""})


@pytest.fixture
def tmp_storage_root(monkeypatch, tmp_path):
    """Redirect the default storage root into a temporary directory."""
    monkeypatch.setattr(doctor._storage, "default_storage_root", lambda: tmp_path)
    return tmp_path


def test_run_doctor_returns_serialisable_dict(tmp_storage_root):
    report = doctor.run_doctor()
    assert isinstance(report, dict)
    assert "ok" in report
    assert "checks" in report
    assert all(
        {"id", "label", "status", "detail", "critical"} <= set(check.keys())
        for check in report["checks"]
    )


def test_python_version_check():
    check = doctor._check_python_version()
    assert check["id"] == "python-version"
    assert check["status"] == doctor._Status.OK
    assert check["critical"] is True


def test_package_check():
    check = doctor._check_package()
    assert check["id"] == "package-import"
    assert check["status"] == doctor._Status.OK
    assert "version" in check["detail"].lower()
    assert check["critical"] is True


def test_dependencies_ok_when_all_present(tmp_storage_root, monkeypatch):
    real_import_module = __import__("importlib").import_module

    def _fake_import(name: str, *args, **kwargs) -> ModuleType:
        if name in ("pymupdf", "httpx", "requests", "pytesseract"):
            return ModuleType(name)
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr("importlib.import_module", _fake_import)
    check = doctor._check_dependencies()
    assert check["status"] == doctor._Status.OK


def test_dependencies_fail_when_required_missing(tmp_storage_root, monkeypatch):
    # Force the source scan to believe pymupdf is imported by the package.
    monkeypatch.setattr(
        doctor, "_imported_third_party_modules", lambda: {"pymupdf"})
    real_import_module = __import__("importlib").import_module

    def _fake_import(name: str, *args, **kwargs) -> ModuleType:
        if name == "pymupdf":
            raise ImportError("no module named pymupdf")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr("importlib.import_module", _fake_import)
    check = doctor._check_dependencies()
    assert check["status"] == doctor._Status.FAIL
    assert check["critical"] is True


def test_tesseract_warn_when_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    check = doctor._check_tesseract()
    assert check["status"] == doctor._Status.WARN


def test_tesseract_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tesseract")
    check = doctor._check_tesseract()
    assert check["status"] == doctor._Status.OK
    assert "/usr/bin/tesseract" in check["detail"]


def test_browser_runtime_warn_when_missing(tmp_storage_root, monkeypatch):
    monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_storage_root)
    check = doctor._check_browser_runtime()
    assert check["status"] == doctor._Status.WARN


def test_browser_runtime_ok_when_found(tmp_storage_root, monkeypatch):
    monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_storage_root)
    (tmp_storage_root / "browser-runtime").mkdir()
    check = doctor._check_browser_runtime()
    assert check["status"] == doctor._Status.OK


def test_sudact_runtime_check_ok(monkeypatch):
    monkeypatch.setattr(
        "case_search.sudact.runtime.runtime_status",
        lambda **_: {"ok": True,
                     "endpoint": "http://127.0.0.1:9377/health",
                     "status": "200", "error": "",
                     "browserConnected": True, "poolSize": 4,
                     "engine": "camofox", "version": "1.2.3"})
    check = doctor._check_sudact_runtime()
    assert check["id"] == "sudact-runtime"
    assert check["status"] == doctor._Status.OK
    assert check["critical"] is False
    assert "up and ready" in check["detail"]


def test_sudact_runtime_check_warn(monkeypatch):
    monkeypatch.setattr(
        "case_search.sudact.runtime.runtime_status",
        lambda **_: {"ok": False,
                     "endpoint": "http://127.0.0.1:9377/health",
                     "status": None,
                     "error": "connection refused",
                     "browserConnected": None, "poolSize": None,
                     "engine": None, "version": None})
    check = doctor._check_sudact_runtime()
    assert check["id"] == "sudact-runtime"
    assert check["status"] == doctor._Status.WARN
    assert check["critical"] is False
    assert "not reachable" in check["detail"]


def test_network_check_classifies_results(tmp_storage_root):
    report = doctor.run_doctor()
    net = next(c for c in report["checks"] if c["id"] == "network-reachability")
    assert net["status"] == doctor._Status.OK
    assert "kad.arbitr.ru" in net["detail"]
    assert "vsrf.ru" in net["detail"]


def test_storage_check_uses_default_root(tmp_storage_root):
    check = doctor._check_storage()
    assert check["status"] == doctor._Status.OK
    assert str(tmp_storage_root.resolve()) in check["detail"]
    assert "writable" in check["detail"].lower()


def test_storage_check_fails_when_not_writable(tmp_storage_root, monkeypatch):
    def _explode(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", _explode)
    check = doctor._check_storage()
    assert check["status"] == doctor._Status.FAIL


def test_license_valid(monkeypatch):
    import case_search.license as lic
    monkeypatch.setattr(lic, "load_license", lambda: {"expires": 1893456000})
    check = doctor._check_license()
    assert check["status"] == doctor._Status.OK


def test_license_missing(monkeypatch):
    from case_search.license import LicenseError
    import case_search.license as lic
    monkeypatch.setattr(lic, "load_license",
                        lambda: (_ for _ in ()).throw(
                            LicenseError("no license key: set CSE_LICENSE_KEY")))
    check = doctor._check_license()
    assert check["status"] == doctor._Status.WARN
    assert check["detail"] == "missing"


def test_license_expired(monkeypatch):
    from case_search.license import LicenseError
    import case_search.license as lic
    monkeypatch.setattr(lic, "load_license",
                        lambda: (_ for _ in ()).throw(
                            LicenseError("license expired 2020-01-01")))
    check = doctor._check_license()
    assert check["status"] == doctor._Status.FAIL
    assert check["detail"] == "expired"


def test_main_doctor_json_output(capsys, tmp_storage_root):
    code = doctor.main_doctor(json_out=True)
    captured = capsys.readouterr()
    assert code == 0
    assert '"checks"' in captured.out


def test_main_doctor_human_output(capsys, tmp_storage_root):
    code = doctor.main_doctor(json_out=False)
    captured = capsys.readouterr()
    assert code == 0
    assert "[OK]" in captured.out or "[WARN]" in captured.out


def test_cli_doctor_subcommand_exists():
    from case_search.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"


def test_cli_doctor_json_flag():
    from case_search.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["doctor", "--json"])
    assert args.doctor_json is True


def test_no_secrets_in_report(tmp_storage_root, monkeypatch):
    """A license key must never leak into the health-check report."""
    from case_search.license import LicenseError
    import case_search.license as lic
    monkeypatch.setattr(lic, "load_license",
                        lambda: (_ for _ in ()).throw(
                            LicenseError("signature mismatch")))
    report = doctor.run_doctor()
    dump = str(report)
    assert "CSE-" not in dump
    assert "license_key" not in dump
