# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Compatibility import for ``sud_cli.case_search.mos``."""

import sys
from sud_cli.case_search import mos as _impl

sys.modules[__name__] = _impl
