"""Case-ref normalization: homoglyph transliteration + space cleanup.

Case numbers are routinely copied from email/messengers where the
Cyrillic letters A, B, C, E, O, P, H, K, M, T, X arrive as visually
identical Latin characters (colleague-legal feedback 1.4). Every parser
normalizes BOTH the requested case_ref and the parsed card number
through `normalize_case_ref` before identity comparison, so a
Latin-typed number matches a Cyrillic document without any fuzzy
matching.
"""

from __future__ import annotations

import re
import unicodedata

# Visually identical Latin -> Cyrillic map (uppercase, case-insensitive
# for letters that exist in both alphabets).
HOMOGLYPHS = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "А", "b": "В", "c": "С", "e": "Е", "h": "Н", "k": "К",
    "m": "М", "o": "О", "p": "Р", "t": "Т", "x": "Х", "y": "У",
})

_NBSP_RE = re.compile(r"[\u00a0\u202f\u2007\u2009\s]+")


def normalize_case_ref(case_ref: str) -> str:
    """Normalize a case reference to canonical Cyrillic form.

    - NFKC unicode normalization (full-width forms -> ASCII);
    - non-breaking / thin spaces -> single regular space, stripped
      at the edges;
    - Latin homoglyphs (A B C E H K M O P T X Y) -> Cyrillic.

    Digits are never transliterated, so '/2025' etc. stay intact.
    """
    if not case_ref:
        return ""
    text = unicodedata.normalize("NFKC", case_ref)
    text = _NBSP_RE.sub(" ", text).strip()
    # Transliterate only the letter prefix (before the first digit or
    # dash), so digits and separators are never touched.
    # Transliterate letters within the number part (before '/'),
    # leaving digits and separators intact. Case numbers never contain
    # meaningful Latin letters after the year slash, so transliterating
    # everything before '/' is safe and handles '2A-...' (digit first).
    head, sep, tail = text.partition("/")
    return head.translate(HOMOGLYPHS) + sep + tail


def refs_match(a: str, b: str) -> bool:
    """Identity comparison after normalization of both sides."""
    return normalize_case_ref(a) == normalize_case_ref(b)


__all__ = ["normalize_case_ref", "refs_match"]
