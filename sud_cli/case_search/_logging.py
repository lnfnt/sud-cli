"""Structured logging for case_search (module-level loggers).

All modules use ``logging.getLogger("case_search.<module>")`` so agents
can configure verbosity without polluting stdout (CLI stdout stays pure
JSON). Diagnostic output goes to stderr by default.
"""

from __future__ import annotations

import logging
import sys

_ROOT_LOGGER = logging.getLogger("case_search")

_HANDLER_ADDED = False


def setup_logging(level: int = logging.DEBUG,
                  stream: "object | None" = None) -> None:
    """Attach a stderr handler to the case_search root logger.

    Called once by CLI ``main()`` or by agent code that wants diagnostic
    output. Idempotent: subsequent calls are no-ops.
    """
    global _HANDLER_ADDED
    target = stream or sys.stderr
    if any(getattr(handler, "stream", None) is target
           for handler in _ROOT_LOGGER.handlers):
        _HANDLER_ADDED = True
        return
    handler = logging.StreamHandler(target)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S"))
    _ROOT_LOGGER.addHandler(handler)
    _ROOT_LOGGER.setLevel(level)
    _HANDLER_ADDED = True


def get_logger(name: str) -> logging.Logger:
    """Return ``case_search.<name>`` logger."""
    return logging.getLogger(f"case_search.{name}")


__all__ = ["setup_logging", "get_logger"]
