# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-2/S3-13: verify source=file — scoped root + size cap.

MCP-агент с произвольным path получал оракул чтения файлов (exact/fuzzy/
offset = бинарный поиск по содержимому); большие файлы — CPU-DoS на
квадратичном fuzzy-проходе.
"""
from __future__ import annotations

import pytest

from sud_cli.verify import VerifyInputError, verify_quote


def test_file_outside_root_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("конфиденциально", encoding="utf-8")
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    with pytest.raises(VerifyInputError, match="S3-2"):
        verify_quote("конфиденциально", "file", path=str(secret),
                     file_root=str(scoped))


def test_file_inside_root_accepted(tmp_path):
    doc = tmp_path / "act.txt"
    doc.write_text("преамбула ТЕСТОВАЯ ЦИТАТА постамбула", encoding="utf-8")
    res = verify_quote("тестовая цитата", "file", path=str(doc),
                       file_root=str(tmp_path))
    assert res["status"] == "exact", res


def test_oversized_file_refused(tmp_path):
    from sud_cli.verify import MAX_VERIFY_FILE_BYTES
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (MAX_VERIFY_FILE_BYTES + 1))
    with pytest.raises(VerifyInputError, match="too large"):
        verify_quote("x", "file", path=str(big), file_root=str(tmp_path))


def test_symlink_escape_refused(tmp_path):
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("секрет за пределами корня", encoding="utf-8")
    link = scoped / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported here")
    with pytest.raises(VerifyInputError, match="S3-2"):
        verify_quote("секрет", "file", path=str(link), file_root=str(scoped))
