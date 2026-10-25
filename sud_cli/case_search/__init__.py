"""Legal Case Search — synthetic core + public agent API.

Public API (lazy import, no external process):

    from case_search import (import_document, search_practice,
                               lookup_case, get_practice_doc)

    outcome = import_document("card.html", "А40-308035/2025")
    if outcome.ok:
        snap = outcome.snapshot        # CaseSnapshot dataclass
        print(snap.case_number, snap.judge)

Live source adapters (KAD, mos-gorsud, sudrf) are NOT_IMPLEMENTED/
QUARANTINE in the registry; network is performed only by
search_practice/get_practice_doc (sudact GET, Camofox for search)
and by agent-injected browser clients.
"""

import sys as _sys

__version__ = "1.2.0"
PARSER_VERSION = "synthetic-core-0.3.0"

# The merged package keeps the old import surface working for case-search's
# modules and for consumers that have not migrated to sud_cli yet.
_sys.modules.setdefault("case_search", _sys.modules[__name__])
from . import mos as _mos
_sys.modules.setdefault("mos", _mos)

_API_NAMES = ("ApiOutcome", "import_document", "lookup_case",
              "search_practice", "search_iter", "get_practice_doc",
              "case_history", "kad_case_history", "kad_search",
              "get_decision_text", "get_sudrf_case",
              "get_sudrf_case_by_vnkod", "get_mos_case")

__all__ = list(_API_NAMES) + ["__version__", "PARSER_VERSION"]


def __getattr__(name: str):
    # Lazy public-API re-export: keeps `from . import PARSER_VERSION`
    # in schema.py free of circular imports while giving agents a
    # single import surface.
    if name in _API_NAMES:
        from . import public_api as _api
        return getattr(_api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
