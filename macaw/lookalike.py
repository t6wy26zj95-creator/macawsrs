"""Find cards in the user's decks that a wrong answer may have been mixed up with.

When the user answers "having spots of different colours" for "recumbent",
that's really the meaning of "mottled" from their own deck. The model can't
see the whole deck, so the code does a cheap word-overlap search and hands it
the few notes whose word or meaning matches the answer.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .templates import normalize

STOPWORDS = set(
    """
    a an the to of in on at by for with from into onto about as and or but nor so if then than that this these those
    it its it's is are was were be been being am do does did done have has had having get gets got
    i me my you your he him his she her we us our they them their one ones someone something somebody
    not no very really quite just like kind kinds sort sorts type types way ways thing things lot lots
    some any all each every other another such more most less much many few various different same
    can could would should will shall may might must mean means meaning meant word use used using
    when where which who whom what why how there here up down out over under again also too only
    i'm i'd don't doesn't think guess maybe probably know dunno sure
    """.split()
)

MIN_TOKEN = 3
MAX_WORD_REPLY = 6  # a reply this short may itself be a word from the deck
MAX_HITS = 3


def _stem(w: str) -> str:
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]  # spotted -> spott -> spot
    if w.endswith("our") and len(w) > 4:
        w = w[:-3] + "or"  # colour -> color
    return w


def tokens(text: str) -> set[str]:
    words = normalize(text).replace("'", " ' ").split()
    return {_stem(w) for w in words if len(w) >= MIN_TOKEN and w not in STOPWORDS and w != "'"}


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and long_.startswith(short)


def _overlap(reply: set[str], side: set[str]) -> int:
    return sum(1 for s in side if any(_same(s, r) for r in reply))


def _matches_meaning(reply: set[str], side: set[str]) -> bool:
    if not reply or not side:
        return False
    hit = _overlap(reply, side)
    need = min(2, len(side), len(reply))
    return hit >= need and hit / min(len(side), len(reply)) >= 0.5


def _has_word(reply_text: str, word: str) -> bool:
    key = normalize(word)
    if len(key) < MIN_TOKEN:
        return False
    return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", normalize(reply_text)) is not None


def find_lookalikes(
    reply_text: str,
    notes: Iterable[Mapping[str, Any]],
    exclude_note: int | None = None,
) -> list[dict[str, Any]]:
    """Notes whose word or meaning matches the reply.

    `notes` rows need `id`, `fields` (list of field names) and `values` (dict).
    Returns at most MAX_HITS dicts with `note_id`, `word`, `meaning`, `deck`.
    """
    reply = tokens(reply_text)
    short_reply = len(normalize(reply_text).split()) <= MAX_WORD_REPLY
    hits: list[tuple[float, dict[str, Any]]] = []
    for n in notes:
        if n["id"] == exclude_note:
            continue
        fields, values = n["fields"], n["values"]
        word = values.get(fields[0], "")
        meaning = values.get(fields[1], "") if len(fields) > 1 else ""
        score = 0.0
        side = tokens(meaning)
        if _matches_meaning(reply, side):
            hit = _overlap(reply, side)
            score = hit + hit / len(side)  # more shared words first, then the closer fit
        elif short_reply and _has_word(reply_text, word):
            score = 3.0  # e.g. answering "mottled" when the card wanted "dappled"
        if score:
            hits.append((score, {"note_id": n["id"], "word": word, "meaning": meaning, "deck": n.get("deck", "")}))
    hits.sort(key=lambda h: -h[0])
    return [h for _, h in hits[:MAX_HITS]]
