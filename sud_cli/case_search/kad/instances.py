"""KAD instance-level detection.

KAD case cards expose tabs for first-instance, appeal (апелляция),
cassation (кассация) and supervision (надзор) cases.  Each tab can carry
its own case-number pattern and page context.  This module detects the
active instance level without loosening validation.
"""

from __future__ import annotations

import re
from typing import Any

# Instance-specific case-number patterns (tight).  Order matters:
# more-specific prefixes must be listed before broader ones so that
# "16АП-1234/2026" is recognised as appeal, not as a first-instance
# number containing "АП-1234/2026".
#
# Negative lookbehinds prevent matching substrings of legitimate numbers
# from another instance (e.g. "40-308035/2025" inside "А40-308035/2025").
APPEAL_NUM_RE = re.compile(r"(?<![0-9])\d{2}АП-\d{1,7}/\d{4}")
CASSATION_NUM_RE = re.compile(
    r"(?<![А-ЯA-Z0-9])\d{2}-\d{4,7}/\d{4}(?:\([^)]*\))?")
SUPERVISION_NUM_RE = re.compile(
    r"(?<![А-ЯA-Z])[А-ЯA-Z]{2,}-\d{1,7}/\d{4}")
FIRST_NUM_RE = re.compile(r"[А-ЯA-Z]\d{0,4}-\d{1,7}/\d{4}")

# Combined case-number regex used for validation.  It must still reject
# garbage such as "banana" or "А40-308035" (no slash/year).
CASE_NUM_RE = re.compile(
    r"(?:(?<![0-9])\d{2}АП-\d{1,7}/\d{4})|"
    r"(?:(?<![А-ЯA-Z0-9])\d{2}-\d{4,7}/\d{4}(?:\([^)]*\))?)|"
    r"(?:(?<![А-ЯA-Z])[А-ЯA-Z]{2,}-\d{1,7}/\d{4})|"
    r"(?:[А-ЯA-Z]\d{0,4}-\d{1,7}/\d{4})"
)

INSTANCE_PATTERNS = (
    ("appeal", APPEAL_NUM_RE),
    ("cassation", CASSATION_NUM_RE),
    ("supervision", SUPERVISION_NUM_RE),
    ("first", FIRST_NUM_RE),
)

INSTANCE_KEYWORDS = {
    "appeal": ("АПЕЛЛЯЦ", "АПЕЛЛЯЦИОННОЙ"),
    "cassation": ("КАССАЦ", "КАССАЦИОННОЙ"),
    "supervision": ("НАДЗОР", "НАДЗОРНОЙ", "СУПЕРВИЗ"),
    "first": ("ПЕРВАЯ ИНСТАНЦ", "ПЕРВОЙ ИНСТАНЦ"),
}


def detect_instance_level(case_number: str,
                          data: dict[str, Any]) -> str:
    """Detect the active KAD instance level.

    Detection uses the case-number pattern first, then falls back to
    explicit page context (status, category, full_text).  Returns one of
    ``first``, ``appeal``, ``cassation``, ``supervision``, or ``unknown``.

    A first-instance number that appears inside an appeal/cassation/supervision
    tab (the original case is often shown as context) is resolved by the
    surrounding page text, not by the number alone.
    """
    text = " ".join([
        case_number,
        data.get("status", ""),
        data.get("category", ""),
        data.get("full_text", "")[:6000],
    ]).upper()

    # Pattern-based detection.
    for level, pattern in INSTANCE_PATTERNS:
        if not pattern.search(case_number):
            continue
        if level != "first":
            return level
        # First-instance numbers are ambiguous: they can appear as the
        # original case number on any tab.  Trust explicit context.
        if any(kw in text for kw in INSTANCE_KEYWORDS["first"]):
            return "first"
        if any(kw in text for kw in INSTANCE_KEYWORDS["appeal"]):
            return "appeal"
        if any(kw in text for kw in INSTANCE_KEYWORDS["cassation"]):
            return "cassation"
        if any(kw in text for kw in INSTANCE_KEYWORDS["supervision"]):
            return "supervision"
        # No contradictory context: default to first instance.
        return "first"

    # No number pattern matched; fall back to context only.
    for level in ("supervision", "cassation", "appeal", "first"):
        if any(kw in text for kw in INSTANCE_KEYWORDS[level]):
            return level

    return "unknown"


__all__ = [
    "APPEAL_NUM_RE",
    "CASSATION_NUM_RE",
    "CASE_NUM_RE",
    "detect_instance_level",
    "FIRST_NUM_RE",
    "INSTANCE_KEYWORDS",
    "INSTANCE_PATTERNS",
    "SUPERVISION_NUM_RE",
]
