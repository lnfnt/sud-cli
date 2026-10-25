"""Stable output formatting for sud-cli."""

import json
import sys


HUMAN = "human"
JSON = "json"


def emit(data, json_mode=False, stream=None):
    """Emit a stable result document to *stream*.

    In JSON mode the document is always a single JSON object with at least:
      - status: "ok" | "error" | "blocked"
      - data:   the payload (None for errors/blocks without payload)
      - meta:   runtime metadata (command, state_dir, version)
    In human mode a concise plain-text rendering is printed.
    """
    if stream is None:
        stream = sys.stdout
    if json_mode:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        stream.write("\n")
    else:
        _render_human(data, stream)


def _render_human(data, stream):
    status = data.get("status")
    payload = data.get("data")
    meta = data.get("meta", {})

    if status == "ok":
        if isinstance(payload, list):
            stream.write(f"{len(payload)} result(s)\n")
            for item in payload:
                stream.write(_format_item(item) + "\n")
        elif isinstance(payload, dict):
            for key, value in sorted(payload.items()):
                stream.write(f"{key}: {value}\n")
        else:
            stream.write(str(payload) + "\n")
    elif status == "blocked":
        stream.write(f"BLOCKED / NOT_IMPLEMENTED: {payload}\n")
    elif status == "error":
        stream.write(f"ERROR: {payload}\n")
    else:
        stream.write(str(payload) + "\n")

    if meta.get("truncated"):
        stream.write("(results truncated)\n")
    if "limit" in meta:
        stream.write(f"limit: {meta['limit']}\n")
    if "total" in meta:
        stream.write(f"total: {meta['total']}\n")


def _format_item(item):
    if isinstance(item, dict):
        return " | ".join(f"{k}={v}" for k, v in sorted(item.items()))
    return str(item)


def result(payload, meta=None, json_mode=False, stream=None):
    meta = meta or {}
    emit({"status": "ok", "data": payload, "meta": meta}, json_mode=json_mode, stream=stream)


def error(message, meta=None, json_mode=False, stream=None, code=1):
    meta = meta or {}
    if stream is None:
        stream = sys.stderr
    emit({"status": "error", "data": message, "meta": meta}, json_mode=json_mode, stream=stream)
    return code


def blocked(message, meta=None, json_mode=False, stream=None):
    """Report a command that was not attempted (gate closed, capability absent).

    Writes to stderr like ``error``: the project convention is that stdout
    carries only successful payloads, so an agent parsing stdout must not meet
    a refusal there.
    """
    meta = meta or {}
    if stream is None:
        stream = sys.stderr
    emit({"status": "blocked", "data": message, "meta": meta}, json_mode=json_mode, stream=stream)
    return 1
