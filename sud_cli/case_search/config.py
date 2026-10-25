"""Central configuration for case-search (production package).

Resolution order (highest first):
1. Environment variables (``CSE_PROXY``, ``CSE_CAMOFOX_URL``, ...)
2. Config file ``~/.config/case-search/config.json``
3. Built-in defaults

No deployment-specific values are hardcoded: defaults are generic, the
operator (or the agent) sets proxy/ports once at install time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_FILE = Path(os.environ.get(
    "CSE_CONFIG",
    Path.home() / ".config" / "case-search" / "config.json"))

_DEFAULTS: dict[str, str] = {
    "proxy": "",                      # e.g. http://user:pass@host:port
    "camofox_url": "",                 # empty -> embedded native browser
    "store_dir": "",                  # snapshot storage (default: cwd)
    "license_key": "",
}

_cache: dict[str, str] | None = None


def _load_file() -> dict[str, str]:
    try:
        return {k: str(v) for k, v in
                json.loads(_CONFIG_FILE.read_text(encoding="utf-8")).items()
                if isinstance(v, (str, int, float))}
    except Exception:
        return {}


def get(key: str, default: str = "") -> str:
    """Config value: env CSE_<KEY> > config file > defaults."""
    global _cache
    if _cache is None:
        _cache = {**_DEFAULTS, **_load_file()}
    env = os.environ.get("CSE_" + key.upper())
    if env is not None:
        return env
    val = _cache.get(key, "")
    return val if val else default


def get_proxy() -> str | None:
    """Configured HTTP proxy or None (direct)."""
    return get("proxy") or None


def get_camofox_url() -> str:
    return get("camofox_url")


def set_value(key: str, value: str, *, save: bool = True) -> None:
    """Persist a config value (used by ``case-search config set``)."""
    global _cache
    data: dict[str, Any] = {}
    if _CONFIG_FILE.is_file():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = value
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    _cache = None  # invalidate


def _mask_url(value: str) -> str:
    """Hide credentials in proxy-style URLs for display paths (audit 25.10 #4)."""
    import re as _re
    return _re.sub(r"://[^/@:]+(?::[^/@]*)?@", "://***:***@", value) if "://" in value else value


def as_dict() -> dict[str, str]:
    """Effective config for display (``case-search config show``).

    NOTE: secrets-masked view — proxy credentials are masked here on purpose.
    Runtime code must use get()/get_proxy() for the real values.
    """
    out = {k: get(k) for k in _DEFAULTS}
    if out.get("proxy"):
        out["proxy"] = _mask_url(out["proxy"])
    return out


# --- Watchlist (original config module) ---
# Watchlist = case refs + optional source names, deliberately boring JSON.

from dataclasses import dataclass  # noqa: E402

DEFAULT_SOURCE = "synthetic"


@dataclass(frozen=True)
class WatchEntry:
    case_ref: str
    source: str = DEFAULT_SOURCE


def load_watchlist(path: Path | str) -> list[WatchEntry]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("watchlist must be a JSON list")
    entries: list[WatchEntry] = []
    for item in data:
        if isinstance(item, str):
            if not item.strip():
                raise ValueError("watchlist case_ref must be non-empty")
            entries.append(WatchEntry(item))
        elif isinstance(item, dict) and "case_ref" in item:
            case_ref = item["case_ref"]
            source = item.get("source", DEFAULT_SOURCE)
            if not isinstance(case_ref, str) or not case_ref.strip():
                raise ValueError(f"invalid watchlist case_ref: {case_ref!r}")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"invalid watchlist source: {source!r}")
            entries.append(WatchEntry(case_ref, source))
        else:
            raise ValueError(f"invalid watchlist entry: {item!r}")
    return entries
