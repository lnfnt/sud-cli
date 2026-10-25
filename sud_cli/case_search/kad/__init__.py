"""KAD Arbitr adapters (browser extract + import mode)."""

from .parser import (KAD_PARSER_VERSION, EXTRACT_EXPRESSION, KadRecord,
                     detect_kad_challenge, fetch_kad_html, parse_kad_data)
from .html_import import (KadHtmlImportProvider,  # noqa: F401
                          fetch_kad_saved_html)
from .search import (KadBrowserOps, KadSearchError,  # noqa: F401
                     KadSearchResult, kad_case_history, search_kad)
from .import_json import KadImportProvider

__all__ = ["KAD_PARSER_VERSION", "EXTRACT_EXPRESSION", "KadRecord",
           "detect_kad_challenge", "fetch_kad_html", "parse_kad_data",
           "KadImportProvider", "KadHtmlImportProvider",
           "fetch_kad_saved_html", "search_kad", "kad_case_history",
           "KadBrowserOps", "KadSearchError", "KadSearchResult"]
