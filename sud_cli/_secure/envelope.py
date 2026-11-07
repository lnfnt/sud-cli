# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Единый JSON-контракт CLI/MCP."""
import json
import re
import sys
import traceback
from typing import Any


class ExitCodes:
    OK = 0
    ERROR = 1
    USAGE = 4


class SchemaValidationError(Exception):
    """Ошибка валидации схемы."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _metadata_from_kw(kw: dict) -> dict:
    meta = kw.pop("metadata", None)
    if meta is None:
        meta = {}
    meta = dict(meta)
    meta.update(kw)
    return meta


def ok(data=None, **kw) -> dict:
    """Возвращает успешный конверт."""
    return {
        "ok": True,
        "status": "ok",
        "data": data,
        "metadata": _metadata_from_kw(kw),
    }


def fail(reason, code=None, **kw) -> dict:
    """Возвращает конверт ошибки."""
    return {
        "ok": False,
        "status": "error",
        "reason": reason,
        "code": code,
        "metadata": _metadata_from_kw(kw),
    }


def make(ok: bool, status=None, reason=None, data=None, metadata=None, **extra) -> dict:
    """Полный конверт the project (ok/status/reason/data/metadata) — обе ветки с data.

    Форма a2a-совместимая: reason и data присутствуют всегда (reason=None при успехе).
    **extra — продуктовые поля конверта (например parsed_at), порядок сохраняется.
    """
    env = {
        "ok": bool(ok),
        "status": status or ("ok" if ok else "error"),
        "reason": reason,
        "data": data,
        "metadata": dict(metadata or {}),
    }
    env.update(extra)
    return env


def print_envelope(env) -> int:
    """Печатает конверт в stdout, возвращает exit-код.

    Ошибочный конверт с явным целочисленным code отдаёт его наружу
    (S3-04: usage-ошибки CLI — rc=4), иначе 0/1 по флагу ok.
    """
    sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    if env.get("ok"):
        return ExitCodes.OK
    code = env.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return ExitCodes.ERROR


def guard_main(fn) -> int:
    """Запускает fn, ловит исключения, печатает fail-конверт."""
    try:
        result = fn()
        if result is None:
            return ExitCodes.OK
        return result
    except Exception as exc:  # noqa: BLE001
        env = fail(str(exc), code=ExitCodes.ERROR)
        print_envelope(env)
        sys.stderr.write("SECURE_CORE_INTERNAL\n")
        traceback.print_exc(file=sys.stderr)
        return ExitCodes.ERROR


def mcp_tool_error(reason, code=-32000) -> dict:
    """Формирует MCP-ошибку инструмента."""
    return {
        "isError": True,
        "reason": reason,
        "code": code,
        "content": [{"type": "text", "text": reason}],
    }


def _type_ok(value: Any, expected) -> bool:
    mapping = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    if isinstance(expected, list):
        return any(_type_ok(value, t) for t in expected)
    py = mapping.get(expected)
    if py is None:
        return True
    # bool is subclass of int; guard
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, py)


def validate_schema(payload, schema: dict) -> None:
    """Проверяет payload по упрощённой JSON-схеме."""
    _validate(payload, schema, "")


def _validate(value, schema, path: str) -> None:
    if not isinstance(schema, dict):
        return
    if "type" in schema and not _type_ok(value, schema["type"]):
        raise SchemaValidationError(f"type mismatch at {path or '/'}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"enum mismatch at {path or '/'}")
    if "pattern" in schema and isinstance(value, str):
        if re.search(schema["pattern"], value) is None:
            raise SchemaValidationError(f"pattern mismatch at {path or '/'}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                raise SchemaValidationError(f"missing required: {r}")
        additional = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}/{k}")
            else:
                if additional is False:
                    raise SchemaValidationError(f"additional property not allowed: {k}")
                if isinstance(additional, dict):
                    _validate(v, additional, f"{path}/{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]")
