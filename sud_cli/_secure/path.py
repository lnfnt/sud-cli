# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Безопасное разрешение путей внутри доверенного корня."""
from pathlib import Path


class PathSecurityError(Exception):
    """Ошибка безопасности пути."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def resolve_inside(base, user_path) -> Path:
    """Разрешает user_path строго внутри base, иначе PathSecurityError."""
    if isinstance(user_path, (bytes, bytearray)):
        user_path = bytes(user_path).decode("utf-8", "surrogateescape")
    elif isinstance(user_path, Path):
        user_path = str(user_path)
    elif not isinstance(user_path, str):
        raise PathSecurityError("invalid_path")
    base = Path(base).resolve()
    p = Path(user_path)
    if p.is_absolute():
        raise PathSecurityError("absolute_path")
    for part in p.parts:
        if part == "..":
            raise PathSecurityError("path_traversal")
    candidate = base / p
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise PathSecurityError("resolve_failed")
    try:
        resolved.relative_to(base)
    except ValueError:
        raise PathSecurityError("symlink_escape")
    if resolved == base:
        raise PathSecurityError("not_inside")
    return resolved
