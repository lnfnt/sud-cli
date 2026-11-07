# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-20: SQLite-соединения трекаются и закрываются пакетно; assert убран."""
from __future__ import annotations

from sud_cli.db import _TRACKED_CONNECTIONS, close_all_connections, get_connection


def test_tracked_connections_closed_by_close_all(tmp_path):
    a = get_connection(tmp_path)
    b = get_connection(tmp_path)
    assert a in _TRACKED_CONNECTIONS and b in _TRACKED_CONNECTIONS
    close_all_connections()
    assert not _TRACKED_CONNECTIONS
    # повторный close идемпотентен
    close_all_connections()


def test_explicit_close_untracks(tmp_path):
    conn = get_connection(tmp_path)
    assert conn in _TRACKED_CONNECTIONS
    conn.close()
    assert conn not in _TRACKED_CONNECTIONS


def test_cli_main_closes_connections(tmp_path):
    from tests.conftest import run_cli
    run_cli(["--json", "search", "--kind", "all", "--query", "test",
             "--state-dir", str(tmp_path)])
    assert not _TRACKED_CONNECTIONS, f"leaked: {len(_TRACKED_CONNECTIONS)}"


def test_fee_refund_negative_paid_rejected():
    from tests.conftest import run_cli
    code, out, err = run_cli(["--json", "fee-refund", "--paid", "-100",
                              "--ground", "withdrawn"])
    assert code == 1
    assert "INVALID_ARGUMENTS" in out + err
