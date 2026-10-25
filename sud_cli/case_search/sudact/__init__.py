"""SudAct.ru adapters (import mode)."""

from .parser import (SUDACT_PARSER_VERSION, SudactImportProvider,
                     SudactRecord, detect_challenge, fetch_sudact_html,
                     parse_sudact_html)
from .search import search as search_practice  # noqa: F401

__all__ = ["SUDACT_PARSER_VERSION", "SudactImportProvider", "SudactRecord",
           "detect_challenge", "fetch_sudact_html", "parse_sudact_html",
           "search_practice"]
