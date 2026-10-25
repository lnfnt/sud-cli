"""Unified retry/backoff for all network-bound sources.

Provides a simple ``retry_with_backoff()`` callable that wraps any
fetcher with exponential backoff + jitter. Replaces the ad-hoc
``time.sleep(30 * (attempt + 1))`` scattered across search_practice.

stdlib-only: uses ``time.sleep`` + ``random`` for jitter.
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from ._logging import get_logger

_log = get_logger("rate_limit")

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 5.0
DEFAULT_MAX_DELAY = 120.0
RATE_LIMIT_INDICATORS = ("429", "rate limit", "too many requests",
                         "retry after")


def is_rate_limited(exc: Exception) -> bool:
    """Check if an exception looks like a rate-limit error."""
    text = str(exc).lower()
    return any(marker in text for marker in RATE_LIMIT_INDICATORS)


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retry_on: Callable[[Exception], bool] | None = None,
) -> T:
    """Call *fn* with exponential backoff on retryable errors.

    Args:
        fn: Zero-argument callable to invoke.
        max_retries: Maximum number of attempts (including the first).
        base_delay: Initial delay in seconds (doubled each retry).
        max_delay: Cap on delay between attempts.
        retry_on: Predicate deciding if an exception is retryable.
                  Defaults to :func:`is_rate_limited`.

    Returns:
        The return value of the first successful call.

    Raises:
        The last exception if all retries are exhausted.
    """
    should_retry = retry_on or is_rate_limited
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not should_retry(exc) or attempt >= max_retries - 1:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = random.uniform(0, delay * 0.3)
            wait = delay + jitter
            _log.debug("retry %d/%d after %.1fs (%s)",
                       attempt + 1, max_retries, wait, exc)
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


__all__ = ["retry_with_backoff", "is_rate_limited",
           "DEFAULT_MAX_RETRIES", "DEFAULT_BASE_DELAY", "DEFAULT_MAX_DELAY"]
