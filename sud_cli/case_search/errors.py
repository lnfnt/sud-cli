# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Fail-closed error hierarchy.

Every classification that is not a verified, parseable, non-empty case payload
must surface as one of these errors or as a degraded status — never as a
successful empty case.
"""


class CourtWatcherError(Exception):
    """Base error."""


class SourceUnavailableError(CourtWatcherError):
    """Adapter is not implemented / quarantined / not configured."""

    def __init__(self, source: str, reason: str = "NOT_IMPLEMENTED"):
        self.source = source
        self.reason = reason
        super().__init__(f"source '{source}' unavailable: {reason}")


class HttpError(CourtWatcherError):
    """Non-success HTTP status from a source."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"http error {status_code}: {detail}")


class WafDetectedError(CourtWatcherError):
    """Captcha / WAF challenge detected. Degraded, never success."""

    def __init__(self, source: str, status_code: int | None = None,
                 retry_after: float | None = None):
        self.source = source
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(
            f"captcha_or_waf for '{source}' (status={status_code}, "
            f"retry_after={retry_after})"
        )


class EmptyPayloadError(CourtWatcherError):
    """Body was empty or whitespace-only. Fail closed."""

    def __init__(self, source: str, detail: str | None = None):
        self.source = source
        self.detail = detail
        msg = f"empty payload from '{source}'"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class OcrUnavailableError(CourtWatcherError):
    """OCR backend could not be loaded or failed at runtime."""

    def __init__(self, source: str, detail: str = ""):
        self.source = source
        self.detail = detail
        msg = f"OCR unavailable for '{source}'"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class MalformedPayloadError(CourtWatcherError):
    """Body was not valid JSON or missing required fields. Fail closed."""

    def __init__(self, source: str, detail: str):
        self.source = source
        super().__init__(f"malformed payload from '{source}': {detail}")


class StorageError(CourtWatcherError):
    """Snapshot store corrupt, unsafe or inconsistent. Fail closed."""


class SudactRuntimeError(CourtWatcherError):
    """Camofox / JS browser runtime for sudact is not reachable."""


class ValidationError(CourtWatcherError):
    """Invalid input parameters (fail-closed)."""

    def __init__(self, source: str, detail: str):
        self.source = source
        self.detail = detail
        super().__init__(f"validation error for '{source}': {detail}")
