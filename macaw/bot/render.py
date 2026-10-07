"""Message text and keyboards for previews, ratings and confirmations."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb

from .. import srs
from ..db import Store, parse
from ..templates import deck_fields, note_fields, render_note_html


def preview(store: Store, proposal_id: int) -> tuple[str, Kb | None]:
    p = store.proposal(proposal_id)
    d = store.deck(p["deck_id"])
    body = render_note_html(deck_fields(d), note_fields(p["fields"]))
    if p["status"] == "added":
        return f"{body}\n\n✅ Added to <b>{html.escape(d['name'])}</b>", None
    if p["status"] == "skipped":
        return f"<s>{body}</s>\n\nSkipped", None
    text = f"New card for <b>{html.escape(d['name'])}</b>:\n\n{body}"
    kb = Kb(
        inline_keyboard=[
            [
                Btn(text="✅ Add", callback_data=f"p:add:{proposal_id}"),
                Btn(text="✏️ Edit", callback_data=f"p:edit:{proposal_id}"),
                Btn(text="Skip", callback_data=f"p:skip:{proposal_id}"),
            ]
        ]
    )
    return text, kb


def rating_note(store: Store, log_id: int, now: datetime | None = None) -> tuple[str, Kb]:
    now = now or datetime.now(timezone.utc)
    r = store.review(log_id)
    after = json.loads(r["after"])
    due = parse(after["due"])
    name = srs.RATING_NAMES[r["rating"]]
    who = "you" if r["source"] == "user" else "Macaw"
    reason = f" · {html.escape(r['reason'])}" if r["reason"] and r["source"] != "user" else ""
    text = f"<i>{who} rated: <b>{name}</b>{reason}\nnext review in {srs.describe_interval(due, now)}</i>"
    row = []
    for value, label in srs.RATING_NAMES.items():
        mark = "• " if value == r["rating"] else ""
        row.append(Btn(text=f"{mark}{label}", callback_data=f"r:{log_id}:{value}"))
    return text, Kb(inline_keyboard=[row])


def confirm_delete(store: Store, note_id: int) -> tuple[str, Kb]:
    note = store.note(note_id)
    d = store.deck(note["deck_id"])
    body = render_note_html(deck_fields(d), note_fields(note["fields"]))
    text = f"Delete this card from <b>{html.escape(d['name'])}</b>?\n\n{body}"
    kb = Kb(
        inline_keyboard=[
            [
                Btn(text="🗑 Delete", callback_data=f"x:yes:{note_id}"),
                Btn(text="Keep", callback_data=f"x:no:{note_id}"),
            ]
        ]
    )
    return text, kb
