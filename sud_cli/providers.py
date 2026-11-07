# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

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
        status="available",
        description=(
            "Live case-search adapters (sudrf/mos/vsrf/npa) are core "
            "functionality and run by default; they need network egress "
            "(standard HTTPS_PROXY/HTTP_PROXY when set) and fail closed with "
            "a typed error naming the cause when it is missing; not a parity "
            "claim."
        ),
    ),
    Provider(
        name="kad-arbitr-ru",
        kind="live",
        status="available",
        description=(
            "KAD/arbitr.ru live case search adapter runs by default and "
            "requires a browser runtime (camofox) for the HTML portal. "
            "Response contract is HTML with a CAPTCHA gate (HTTP 451 on "
            "direct probes); the parser is best-effort, not a parity claim."
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
