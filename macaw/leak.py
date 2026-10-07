"""Detect when a card's answer shows up in conversation before it was asked."""

from __future__ import annotations

import re

from .templates import normalize

MIN_KEY_LEN = 3


def contains_answer(text: str, answer: str) -> bool:
    """True if the (normalized) answer appears as whole words in the text."""
    key = normalize(answer)
    if len(key) < MIN_KEY_LEN:
        return False
    hay = normalize(text)
    return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", hay) is not None
