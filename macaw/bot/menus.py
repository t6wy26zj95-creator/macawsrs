"""The /decks menu. Everything lives in one message that is edited on each tap.

Callback data starts with "m:".
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb

from .. import srs
from ..db import Store, parse
from ..templates import DECK_TYPES, card_sides, deck_fields, note_fields, render_note_html

PAGE = 8


def _kb(rows: list[list[tuple[str, str]]]) -> Kb:
    return Kb(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row] for row in rows])


def deck_list(store: Store, user_id: int) -> tuple[str, Kb]:
    decks = store.decks(user_id)
    user = store.get_user(user_id)
    queue = srs.due_today(store, user, datetime.now(timezone.utc))
    if not decks:
        return (
            "You have no decks yet. Tell me what you're learning, for example "
            "<i>make an English vocabulary deck</i>.",
            _kb([[("Close", "m:close")]]),
        )
    rows = []
    for d in decks:
        due = sum(1 for c in queue if c["deck_id"] == d["id"])
        label = f"{d['name']} · {store.count_notes(d['id'])} cards" + (f" · {due} due" if due else "")
        rows.append([(label, f"m:deck:{d['id']}")])
    rows.append([("Close", "m:close")])
    return "<b>Your decks</b>", _kb(rows)


def deck_view(store: Store, deck_id: int) -> tuple[str, Kb]:
    d = store.deck(deck_id)
    user = store.get_user(d["user_id"])
    st = srs.deck_stats(store, user, datetime.now(timezone.utc)).get(deck_id, srs.empty_stats())
    label = DECK_TYPES.get(d["deck_type"], {}).get("label", d["deck_type"])
    notes = store.count_notes(deck_id)
    cards = st["new"] + st["learning"] + st["young"] + st["mature"]
    text = (
        f"<b>{html.escape(d['name'])}</b>\n"
        f"Type: {html.escape(label)}\n"
        f"Cards: {notes}" + (f" ({cards} with reverse cards)" if cards != notes else "") + "\n"
        f"New {st['new']} · learning {st['learning']} · young {st['young']} · mature {st['mature']}\n"
        f"Due today: {st['due']}\n"
        f"Daily limits: {d['new_per_day']} new, {d['reviews_per_day']} reviews\n"
        f"Reverse cards: {'on' if d['reverse'] else 'off'}\n\n"
        f"<i>Young cards come back within 3 weeks, mature ones after 3 weeks or more.</i>"
    )
    return text, _kb(
        [
            [("Cards", f"m:cards:{deck_id}:0"), ("Due today", f"m:due:{deck_id}:0")],
            [("Limits", f"m:lim:{deck_id}"), ("Template", f"m:tmpl:{deck_id}")],
            [("Rename", f"m:ren:{deck_id}"), ("Export to Anki", f"exp:{deck_id}")],
            [("Delete deck", f"m:del:{deck_id}")],
            [("« Decks", "m:list")],
        ]
    )


DUE_PAGE = 20


def due_list(store: Store, deck_id: int, page: int) -> tuple[str, Kb]:
    """Cards of the deck still due today, prompt side only so no answer is given away."""
    d = store.deck(deck_id)
    user = store.get_user(d["user_id"])
    now = datetime.now(timezone.utc)
    due_now = [c for c in srs.due_queue(store, user, now) if c["deck_id"] == deck_id]
    later = [c for c in srs.coming_back_today(store, user, now) if c["deck_id"] == deck_id]
    items = [(c, "") for c in due_now] + [(c, " (back later today)") for c in later]
    pages = max(1, (len(items) + DUE_PAGE - 1) // DUE_PAGE)
    page = min(max(0, page), pages - 1)
    fields = deck_fields(d)
    lines = [f"<b>{html.escape(d['name'])}</b> · due today: {len(items)}"]
    if not items:
        lines.append("\nNothing left for today.")
    for c, note in items[page * DUE_PAGE : (page + 1) * DUE_PAGE]:
        prompt, _ = card_sides(fields, note_fields(c["note_fields"]), c["ord"])
        kind = "new" if c["state"] == srs.NEW else "review" if c["state"] == 2 else "learning"
        lines.append(f"• {html.escape(prompt.splitlines()[0] if prompt else '(empty)')[:60]} <i>{kind}{note}</i>")
    if any(c["state"] == srs.NEW for c, _ in items):
        lines.append("\n<i>New cards are counted up to the deck's daily limit.</i>")
    nav = []
    if page > 0:
        nav.append(("‹ Prev", f"m:due:{deck_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Next ›", f"m:due:{deck_id}:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([("« Back", f"m:deck:{deck_id}")])
    return "\n".join(lines), _kb(rows)


def card_list(store: Store, deck_id: int, page: int) -> tuple[str, Kb]:
    d = store.deck(deck_id)
    total = store.count_notes(deck_id)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(0, page), pages - 1)
    notes = store.notes_in_deck(deck_id, page * PAGE, PAGE)
    fields = deck_fields(d)
    rows = []
    for n in notes:
        prompt, answer = card_sides(fields, note_fields(n["fields"]), 0)
        label = f"{prompt} — {answer.splitlines()[0] if answer else ''}"
        rows.append([(label[:60], f"m:note:{n['id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(("‹ Prev", f"m:cards:{deck_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Next ›", f"m:cards:{deck_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("« Back", f"m:deck:{deck_id}")])
    text = f"<b>{html.escape(d['name'])}</b> · cards (page {page + 1}/{pages})"
    if not notes:
        text += "\n\nNo cards yet. Ask me in chat to add some."
    return text, _kb(rows)


def note_view(store: Store, note_id: int, page: int) -> tuple[str, Kb]:
    n = store.note(note_id)
    d = store.deck(n["deck_id"])
    body = render_note_html(deck_fields(d), note_fields(n["fields"]))
    lines = [body, ""]
    now = datetime.now(timezone.utc)
    for c in store.cards_for_note(note_id):
        side = "reverse" if c["ord"] == 1 else "forward"
        if c["state"] == srs.NEW:
            lines.append(f"<i>{side}: new</i>")
        else:
            due = parse(c["due"])
            when = "now" if due <= now else f"in {srs.describe_interval(due, now)}"
            lines.append(f"<i>{side}: due {when}</i>")
    lines.append("\nTo change this card, tell me in chat.")
    return "\n".join(lines), _kb(
        [
            [("Delete", f"m:ndel:{note_id}:{page}")],
            [("« Back", f"m:cards:{d['id']}:{page}")],
        ]
    )


def note_delete_confirm(store: Store, note_id: int, page: int) -> tuple[str, Kb]:
    text, _ = note_view(store, note_id, page)
    return text + "\n\n<b>Delete this card?</b>", _kb(
        [[("Yes, delete", f"m:ndelok:{note_id}:{page}"), ("No", f"m:note:{note_id}:{page}")]]
    )


def limits(store: Store, deck_id: int) -> tuple[str, Kb]:
    d = store.deck(deck_id)
    text = (
        f"<b>{html.escape(d['name'])}</b> · daily limits\n\n"
        f"New cards per day: <b>{d['new_per_day']}</b>\n"
        f"Reviews per day: <b>{d['reviews_per_day']}</b>"
    )
    return text, _kb(
        [
            [("New −5", f"m:limset:{deck_id}:n:-5"), ("New +5", f"m:limset:{deck_id}:n:5")],
            [("Reviews −50", f"m:limset:{deck_id}:r:-50"), ("Reviews +50", f"m:limset:{deck_id}:r:50")],
            [("« Back", f"m:deck:{deck_id}")],
        ]
    )


def template(store: Store, deck_id: int) -> tuple[str, Kb]:
    d = store.deck(deck_id)
    fields = deck_fields(d)
    lines = [f"<b>{html.escape(d['name'])}</b> · card template", ""]
    for i, f in enumerate(fields):
        role = "prompt" if i == 0 else "answer" if i == 1 else "extra"
        lines.append(f"{i + 1}. {html.escape(f)} <i>({role})</i>")
    lines.append(f"\nReverse cards: {'on' if d['reverse'] else 'off'}")
    lines.append("\nTo add, remove or rename fields, tell me in chat, e.g. <i>add a gender field to this deck</i>.")
    return "\n".join(lines), _kb(
        [
            [("Turn reverse " + ("off" if d["reverse"] else "on"), f"m:rev:{deck_id}")],
            [("« Back", f"m:deck:{deck_id}")],
        ]
    )


def delete_confirm(store: Store, deck_id: int) -> tuple[str, Kb]:
    d = store.deck(deck_id)
    n = store.count_notes(deck_id)
    return (
        f"Delete <b>{html.escape(d['name'])}</b> and its {n} cards? This can't be undone.",
        _kb([[("Yes, delete", f"m:delok:{deck_id}"), ("No", f"m:deck:{deck_id}")]]),
    )


def rename_prompt(store: Store, deck_id: int) -> tuple[str, Kb]:
    d = store.deck(deck_id)
    return (
        f"Send me the new name for <b>{html.escape(d['name'])}</b>.",
        _kb([[("Cancel", f"m:deck:{deck_id}")]]),
    )
