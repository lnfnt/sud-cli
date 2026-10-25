"""Transport-agnostic court-source adapter contract.

Phase 3 defines the contract and provides one offline synthetic adapter only.
No live court source is implemented.
"""

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

STATUS_OK = "ok"
STATUS_NO_RESULTS = "no_results"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class CourtProviderResult:
    """Normalized result from any court-source adapter."""

    status: str
    items: List[Dict[str, Any]]
    source: str
    source_url: Optional[str] = None
    fetched_at: Optional[str] = None
    pagination: Dict[str, Any] = field(
        default_factory=lambda: {
            "page": 1,
            "page_size": 10,
            "total": 0,
            "pages_total": 0,
            "limit": 10,
        }
    )
    error_message: Optional[str] = None


class CourtSource(Protocol):
    """Transport-agnostic adapter protocol for live and offline court sources."""

    def fetch_case(self, case_id: str, **kwargs: Any) -> CourtProviderResult:
        ...

    def search_cases(
        self, query: str, page: int = 1, page_size: int = 10, **kwargs: Any
    ) -> CourtProviderResult:
        ...


class LocalJsonCourtSource:
    """Offline synthetic fixture source.

    Reads local JSON files in the native ``cases``/``events`` schema and
    returns a typed ``CourtProviderResult``. No network access is performed.
    """

    def __init__(self, source_label: str = "local-synthetic-json"):
        self.source_label = source_label

    def import_file(self, path: Path) -> CourtProviderResult:
        from sud_cli.importing import CourtImportError, load_court_case_fixture

        try:
            cases = load_court_case_fixture(Path(path))
        except CourtImportError as exc:
            return CourtProviderResult(
                status=STATUS_ERROR,
                items=[],
                source=self.source_label,
                error_message=str(exc),
            )
        except Exception as exc:
            return CourtProviderResult(
                status=STATUS_ERROR,
                items=[],
                source=self.source_label,
                error_message=f"unexpected import error: {exc}",
            )

        if not cases:
            return CourtProviderResult(
                status=STATUS_NO_RESULTS,
                items=[],
                source=self.source_label,
                error_message="no cases in fixture",
            )

        return CourtProviderResult(
            status=STATUS_OK,
            items=cases,
            source=self.source_label,
            fetched_at=_utc_now(),
            pagination={
                "page": 1,
                "page_size": len(cases),
                "total": len(cases),
                "pages_total": 1,
                "limit": len(cases),
            },
        )


def _utc_now() -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc).replace(microsecond=0).isoformat()
    )
