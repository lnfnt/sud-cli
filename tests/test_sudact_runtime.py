"""Tests for ``case_search.sudact.runtime``.

No real browser sessions are started; probes target a closed localhost port.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import SudactRuntimeError  # noqa: E402
from case_search.sudact import runtime  # noqa: E402


def _closed_port() -> int:
    """Return a free localhost port that is not accepting connections."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_unreachable_returns_ok_false_dict():
    port = _closed_port()
    result = runtime.probe_browser_runtime(
        f"http://127.0.0.1:{port}", timeout=0.5)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["endpoint"] == f"http://127.0.0.1:{port}/health"
    assert result["error"]


def test_probe_never_raises():
    result = runtime.probe_browser_runtime(
        "http://127.0.0.1:1", timeout=0.1)
    assert result["ok"] is False


def test_js_fetcher_raises_when_runtime_down():
    port = _closed_port()
    with pytest.raises(SudactRuntimeError) as ctx:
        runtime.js_fetcher(f"http://127.0.0.1:{port}", timeout=0.5)
    msg = str(ctx.value)
    assert "browser runtime not available" in msg
    assert "Start the camofox runtime service" in msg
    assert f"127.0.0.1:{port}" in msg


def test_cli_search_practice_exits_2_when_runtime_unreachable(
        monkeypatch, capsys):
    import case_search.license as lic
    from case_search.cli import main

    # Skip the license gate so the probe is reached.
    monkeypatch.setattr(lic, "load_license",
                        lambda: {"expires": 1893456000})

    port = _closed_port()
    code = main([
        "search-practice", "договор подряда",
        "--runtime-url", f"http://127.0.0.1:{port}",
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "browser runtime not available" in captured.err
    assert "Start the camofox runtime service" in captured.err
    assert f"127.0.0.1:{port}" in captured.err


def test_cli_case_history_exits_2_when_runtime_unreachable(
        monkeypatch, capsys):
    import case_search.license as lic
    from case_search.cli import main

    monkeypatch.setattr(lic, "load_license",
                        lambda: {"expires": 1893456000})

    port = _closed_port()
    code = main([
        "case-history", "А19-30387/2025",
        "--runtime-url", f"http://127.0.0.1:{port}",
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "browser runtime not available" in captured.err


def test_cli_kad_case_history_exits_2_when_runtime_unreachable(
        monkeypatch, capsys):
    import case_search.license as lic
    from case_search.cli import main

    monkeypatch.setattr(lic, "load_license",
                        lambda: {"expires": 1893456000})

    port = _closed_port()
    code = main([
        "kad-case-history", "А19-30387/2025",
        "--runtime-url", f"http://127.0.0.1:{port}",
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "browser runtime not available" in captured.err


class _RuntimeStub(BaseHTTPRequestHandler):
    """Configurable loopback stub for the Camofox runtime API."""

    tab_calls: int = 0
    tab_failures: int = 0
    health_body: bytes = b"{}"

    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(self.health_body)
        else:
            self.send_response(404)
            self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)

    def do_POST(self):
        self._read_body()
        if self.path == "/tabs":
            _RuntimeStub.tab_calls += 1
            if _RuntimeStub.tab_failures > 0:
                _RuntimeStub.tab_failures -= 1
                body = ("browserContext.newPage: Protocol error "
                        "(Browser.newPage): window is null").encode("utf-8")
                self.send_response(500)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"tabId": "tab-1"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def runtime_stub():
    """Yield the base URL of a freshly reset loopback runtime stub."""
    _RuntimeStub.tab_calls = 0
    _RuntimeStub.tab_failures = 0
    _RuntimeStub.health_body = json.dumps({
        "ok": True,
        "browserConnected": True,
        "poolSize": 4,
        "engine": "camofox",
        "version": "1.2.3",
    }).encode("utf-8")

    server = HTTPServer(("127.0.0.1", 0), _RuntimeStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_open_tab_retry_succeeds_on_second_attempt(runtime_stub):
    _RuntimeStub.tab_failures = 1
    fetcher = runtime.js_fetcher(runtime_stub, timeout=5.0)
    # js_fetcher returns a fetch closure; just opening it exercises the
    # probe + tab creation path.  We call fetch with a URL that will 404
    # on navigate, but the retry logic is proven by reaching navigate.
    with pytest.raises(runtime.FetchError):
        fetcher("http://127.0.0.1/some-page")
    assert _RuntimeStub.tab_calls >= 2


def test_open_tab_retry_exhausted_raises_actionable_error(runtime_stub):
    _RuntimeStub.tab_failures = 10  # always fail
    _RuntimeStub.health_body = json.dumps({
        "ok": True,
        "browserConnected": False,
        "poolSize": 0,
    }).encode("utf-8")

    fetcher = runtime.js_fetcher(runtime_stub, timeout=5.0)
    with pytest.raises(SudactRuntimeError) as ctx:
        fetcher("http://example.com")
    msg = str(ctx.value)
    assert "browser runtime is up but its browser died" in msg
    assert "Restart the camofox service" in msg
    assert "browserConnected=False" in msg
    assert "poolSize=0" in msg


def test_runtime_status_parses_health_json(runtime_stub):
    status = runtime.runtime_status(runtime_stub, timeout=2.0)
    assert status["ok"] is True
    assert status["browserConnected"] is True
    assert status["poolSize"] == 4
    assert status["engine"] == "camofox"
    assert status["version"] == "1.2.3"


def test_doctor_detail_includes_browser_connected_when_down(
        monkeypatch, runtime_stub):
    from case_search import doctor as _doctor

    _RuntimeStub.health_body = json.dumps({
        "ok": True,
        "browserConnected": False,
        "poolSize": 0,
    }).encode("utf-8")

    monkeypatch.setattr(runtime, "DEFAULT_BASE_URL", runtime_stub)
    check = _doctor._check_sudact_runtime()
    assert check["status"] == _doctor._Status.WARN
    assert "browserConnected=False" in check["detail"]
    assert "poolSize=0" in check["detail"]
    assert "up but browser dead" in check["detail"]


def test_cli_case_history_prints_doctor_hint_on_runtime_error(
        monkeypatch, capsys, runtime_stub):
    import case_search.license as lic
    from case_search.cli import main

    monkeypatch.setattr(lic, "load_license",
                        lambda: {"expires": 1893456000})
    _RuntimeStub.tab_failures = 10
    _RuntimeStub.health_body = json.dumps({
        "ok": True,
        "browserConnected": False,
        "poolSize": 0,
    }).encode("utf-8")

    code = main([
        "case-history", "А19-30387/2025",
        "--runtime-url", runtime_stub,
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "browser runtime is up but its browser died" in captured.err
    assert "Restart the camofox service" in captured.err
    assert "python3 -m case_search doctor | grep runtime" in captured.err
    assert "browserConnected=False" in captured.err
