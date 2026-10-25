"""Source/provider registry for sud-cli."""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str  # 'offline' | 'live'
    status: str  # 'available' | 'unavailable' | 'blocked'
    description: str


REGISTRY: List[Provider] = [
    Provider(
        name="local-json-court",
        kind="offline",
        status="available",
        description="Synthetic local JSON import for legacy court records.",
    ),
    Provider(
        name="local-json-court-case",
        kind="offline",
        status="available",
        description="Synthetic local JSON import for native court case/event timelines.",
    ),
    Provider(
        name="local-json-npa",
        kind="offline",
        status="available",
        description="Synthetic local JSON import for NPA document fixtures.",
    ),
    Provider(
        name="case-search-live",
        kind="live",
        status="unavailable",
        description="Live court source adapter; not implemented (Phase 3 offline-only).",
    ),
    Provider(
        name="case-search-bridge",
        kind="bridge",
        status="available",
        description=(
            "Transitional read-only bridge to the case-search CLI for allowlisted "
            "offline import/extraction commands; not a live source or parity claim."
        ),
    ),
    Provider(
        name="kad-arbitr-ru",
        kind="live",
        status="unavailable",
        description=(
            "KAD/arbitr.ru live case search; response contract is unverified "
            "(HTML response, CAPTCHA gate, HTTP 451 probe) and no live adapter is implemented."
        ),
    ),
    Provider(
        name="pravo-gov-ru",
        kind="live",
        status="available",
        description=(
            "Official publication.pravo.gov.ru metadata-only search; "
            "one bounded GET per invocation, HTTPS only."
        ),
    ),
    Provider(
        name="garant",
        kind="live",
        status="unavailable",
        description="Commercial legal source metadata-only access; not implemented in Phase 1.",
    ),
    Provider(
        name="consultantplus",
        kind="live",
        status="unavailable",
        description="Commercial legal source metadata-only access; not implemented in Phase 1.",
    ),
    Provider(
        name="fees-calculator",
        kind="calculator",
        status="blocked",
        description="Court fee calculation; blocked in Phase 1.",
    ),
    Provider(
        name="deadlines-calculator",
        kind="calculator",
        status="blocked",
        description="Procedural deadline calculation; blocked in Phase 1.",
    ),
]


def list_providers() -> List[Provider]:
    return list(REGISTRY)
