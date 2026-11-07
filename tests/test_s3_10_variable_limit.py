# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-10: файл ровно на 1000 записей не должен падать SQLITE_MAX_VARIABLES."""
from __future__ import annotations

import json


def test_import_1000_records_not_variable_limit(tmp_path):
    from sud_cli.db import get_connection, init_db
    from sud_cli.importing import import_court_cases

    cases = [{
        "case_id": f"case-{i:04d}",
        "case_number": f"А40-{i}/2026",
        "court": "АС Москвы",
        "plaintiff": "Истец",
        "defendant": "Ответчик",
        "source_name": "probe",
        "imported_at": "2026-11-01",
    } for i in range(1000)]
    f = tmp_path / "batch.json"
    f.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")

    init_db(tmp_path)
    conn = get_connection(tmp_path)
    res = import_court_cases(conn, f)
    assert res.get("ok") is not False, res
    assert res.get("inserted") == 1000, res
