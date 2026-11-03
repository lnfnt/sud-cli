# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-7: create_case — конкурентная гонка не даёт двух дел с одним
(number, court): уникальный индекс + IntegrityError → duplicate_window."""
from __future__ import annotations


def test_create_case_duplicate_via_race_window(tmp_path):
    from sud_cli.db import create_case, get_connection, init_db
    init_db(tmp_path)
    conn = get_connection(tmp_path)
    r1 = create_case(conn, "А40-123/2026", "АС Москвы")
    assert r1["created"] is True
    # повторный вызов (в т.ч. имитация выигравшего гонку конкурента)
    r2 = create_case(conn, "А40-123/2026", "АС Москвы")
    assert r2["created"] is False and r2["duplicate"] is True
    assert r2["case_id"] == r1["case_id"]
    # индекс реально стоит: прямой INSERT дубля отклоняется
    import sqlite3
    with __import__("pytest").raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cases (case_id, case_number, court, source_name, imported_at)"
            " VALUES ('x2', 'А40-123/2026', 'АС Москвы', 'probe', '2026-11-01')")
        conn.commit()
