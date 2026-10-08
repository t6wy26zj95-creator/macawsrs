"""Deck types, card templates and text helpers.

A deck has an ordered list of field names. The first field is the prompt
side and the second is the answer. Further fields are extra information
shown with the answer (example sentence, notes, ...). A deck with
`reverse` on gets a second card per note that asks answer -> prompt.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any, Mapping

DECK_TYPES: dict[str, dict[str, Any]] = {
    "vocabulary": {
        "label": "Vocabulary",
        "fields": ["word", "meaning", "example", "notes"],
        "hint": "word or phrase, its meaning, one natural example sentence, short usage notes",
    },
    "basic": {
        "label": "Basic (question and answer)",
        "fields": ["front", "back", "notes"],
        "hint": "a question or prompt, its answer, optional notes",
    },
}


def deck_fields(deck: Mapping[str, Any]) -> list[str]:
    return json.loads(deck["fields"]) if isinstance(deck["fields"], str) else list(deck["fields"])


def note_fields(raw: str | Mapping[str, str]) -> dict[str, str]:
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def normalize(text: str) -> str:
    """Lowercase, strip accents' combining marks off punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"^(to|a|an|the)\s+", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def sort_key(fields: list[str], values: Mapping[str, str]) -> str:
    return normalize(values.get(fields[0], ""))


def clean_fields(fields: list[str], values: Mapping[str, Any]) -> dict[str, str]:
    """Keep only the deck's fields, as stripped strings."""
    return {f: str(values.get(f) or "").strip() for f in fields}


def card_sides(fields: list[str], values: Mapping[str, str], ord_: int) -> tuple[str, str]:
    """Return (prompt, answer) for a card. Extra fields are appended to the answer."""
    first = values.get(fields[0], "")
    second = values.get(fields[1], "") if len(fields) > 1 else ""
    extras = [f"{f}: {values[f]}" for f in fields[2:] if values.get(f)]
    if ord_ == 1:
        prompt, answer = second, first
    else:
        prompt, answer = first, second
    if extras:
        answer = answer + "\n" + "\n".join(extras)
    return prompt, answer


def headword(prompt: str) -> tuple[str, str]:
    """Split a prompt side into its first line and the rest. Imported Anki cards
    often have an example sentence under the word on the front."""
    first, _, rest = (prompt or "").strip().partition("\n")
    return first.strip(), rest.strip()


def answer_key(fields: list[str], values: Mapping[str, str], ord_: int) -> str:
    """The text that must not be revealed before the card is asked."""
    if ord_ == 1:
        return values.get(fields[0], "")
    return values.get(fields[1], "") if len(fields) > 1 else ""


def render_note_html(fields: list[str], values: Mapping[str, str]) -> str:
    lines = []
    for i, f in enumerate(fields):
        v = values.get(f, "")
        if not v:
            continue
        if i == 0:
            lines.append(f"<b>{html.escape(v)}</b>")
        else:
            lines.append(f"<i>{html.escape(f)}:</i> {html.escape(v)}")
    return "\n".join(lines) or "<i>(empty)</i>"
