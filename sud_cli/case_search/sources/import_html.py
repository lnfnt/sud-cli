"""Local-file import provider: user saves HTML from their browser.

Reads a single HTML file from disk and feeds it through the existing
Mos-Gorsud DOM parser. No network I/O. Case number must be provided
explicitly by the caller.
"""

from __future__ import annotations

from pathlib import Path

from .base import Provider, RawSourceResult
from ..errors import SourceUnavailableError
from ..normalize_refs import refs_match


class MosDomImportProvider(Provider):
    """Local import adapter: one HTML file -> RawSourceResult."""

    name = "mos-gorsud-import"
    media_type = "text/html"
    accepts_fixtures = False

    def __init__(self, html_path: Path | str, case_ref: str):
        self.html_path = Path(html_path)
        self.case_ref = case_ref

    def lookup(self, case_ref: str) -> RawSourceResult:
        if not refs_match(case_ref, self.case_ref):
            raise SourceUnavailableError(
                self.name,
                f"requested '{case_ref}' but import was staged for "
                f"'{self.case_ref}'")
        if not self.html_path.is_file():
            raise SourceUnavailableError(
                self.name, f"import file missing: {self.html_path}")
        body = self.html_path.read_text(encoding="utf-8")
        return RawSourceResult(
            source=f"{self.name}:{self.html_path.name}",
            source_url=f"file://{self.html_path.resolve()}",
            status_code=200,
            body=body,
            media_type="text/html",
        )


__all__ = ["MosDomImportProvider"]
