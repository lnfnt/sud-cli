# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>
"""Compat-shim: netguard re-export.

Canonical implementation is vendored at ``sud_cli/_vendor/lex_std/netguard.py``
(the public tree ships the vendored copy; the fallback keeps environments working
where the external ``lex_std`` package is already installed).
"""
from __future__ import annotations

try:  # vendored copy shipped with this tree
    from sud_cli._vendor.lex_std.netguard import *  # noqa: F401,F403
    from sud_cli._vendor.lex_std.netguard import (  # noqa: F401
    NETGUARD_VERSION, EGRESS_STATE, EgressBlockedError, BreakerOpenError,
    _DEFAULT_DB, _normalize_proxy_url, _egress_proxy, _applies_to, _breaker_db,
    _allow_direct, _breaker_suffixes, guard_url, guarded_get,
    check_before_fetch, breaker_remaining, breaker_trip,
    breaker_soft_fail, breaker_success, spotlight, is_spotlighted, strip_spotlight,

    )
except ImportError:  # pragma: no cover - external lex_std installation
    from lex_std.netguard import *  # noqa: F401,F403
    from lex_std.netguard import (  # noqa: F401
    NETGUARD_VERSION, EGRESS_STATE, EgressBlockedError, BreakerOpenError,
    _DEFAULT_DB, _normalize_proxy_url, _egress_proxy, _applies_to, _breaker_db,
    _allow_direct, _breaker_suffixes, guard_url, guarded_get,
    check_before_fetch, breaker_remaining, breaker_trip,
    breaker_soft_fail, breaker_success, spotlight, is_spotlighted, strip_spotlight,

    )
