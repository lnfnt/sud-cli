# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Атомарная запись файлов."""
import json
import os
import tempfile
from pathlib import Path


def write_atomic(path, content, mode=0o644) -> None:
    """Атомарно записывает content в path."""
    path = Path(path)
    directory = path.parent
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = bytes(content)
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
        try:
            dfd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def write_atomic_json(path, obj, mode=0o644) -> None:
    """Атомарно записывает obj как JSON."""
    text = json.dumps(obj, ensure_ascii=False) + "\n"
    write_atomic(path, text, mode=mode)
