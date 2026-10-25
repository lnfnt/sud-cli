"""Shared utilities for sudact parser and search modules."""

from __future__ import annotations

import re

CHALLENGE_MARKERS = (
    "captcha",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "ddos-guard",
)


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.replace("&nbsp;", " ")
                    .replace("&amp;", "&").split())


__all__ = ["CHALLENGE_MARKERS", "strip_tags"]
