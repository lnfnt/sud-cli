# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>
"""Compat-shim: netguard re-export.

Canonical implementation ships with this tree at ``sud_cli/_secure/netguard.py``
(vendored copy of the project's shared network guard).
"""
from __future__ import annotations

from sud_cli._secure.netguard import *  # noqa: F401,F403
from sud_cli._secure.netguard import (  # noqa: F401
NETGUARD_VERSION, EGRESS_STATE, EgressBlockedError, BreakerOpenError,
    _DEFAULT_DB, _normalize_proxy_url, _egress_proxy, _applies_to, _breaker_db,
    _allow_direct, _breaker_suffixes, guard_url, guarded_get,
    check_before_fetch, breaker_remaining, breaker_trip,
    breaker_soft_fail, breaker_success, spotlight, is_spotlighted, strip_spotlight,

)
