"""The conversation engine: builds Claude's context and tools for each turn and
turns the result into actions for the Telegram layer.

Claude can only change state through the tools defined here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from . import pacing, srs
from .db import Store, iso, parse
from .leak import contains_answer
from .llm import LLMProvider, Models, ToolSpec
from .prompts import SYSTEM_PROMPT
from .templates import (
    DECK_TYPES,
    answer_key,
    card_sides,
    clean_fields,
    deck_fields,
    note_fields,
    sort_key,
)

log = logging.getLogger(__name__)

# Emoji and pictographs, plus the joiners and variation selectors that glue them together.
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def strip_emoji(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", EMOJI.sub("", text)).strip()


# ---------- actions the Telegram layer performs after a turn ----------


@dataclass
class Text:
    text: str


@dataclass
class Preview:
    proposal_id: int
    replaces: tuple[int, ...] = ()  # message ids of older previews to remove from the chat


@dataclass
class RatingNote:
    log_id: int


@dataclass
class ConfirmDelete:
    note_id: int


Action = Text | Preview | RatingNote | ConfirmDelete


@dataclass
class Turn:
    user: dict[str, Any]
    now: datetime
    actions: list[Action] = field(default_factory=list)
    graded: set[int] = field(default_factory=set)
    postponed: set[int] = field(default_factory=set)
    continued: bool = False  # a grade was followed by the next card in the same turn


def _jsonschema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# Keep each Claude turn small so long chats don't eat into the Pro usage limits:
# only the latest messages go in, long ones are shortened, and the due list is capped.
HISTORY_MESSAGES = 16
MAX_MESSAGE_CHARS = 500
DUE_LIST_LIMIT = 15

STR = {"type": "string"}
INT = {"type": "integer"}


class Agent:
    def __init__(self, store: Store, llm: LLMProvider, models: Models | None = None):
        self.store = store
        self.llm = llm
        self.models = models  # when set, picks each user's model; otherwise everyone uses `llm`

    # ================= public entry points =================

    async def on_user_message(self, user_id: int, text: str, now: datetime | None = None) -> list[Action]:
        now = now or datetime.now(timezone.utc)
        self.store.log_message(user_id, "user", text)
        st = self.store.state(user_id)
        self.store.update_state(user_id, last_user_at=iso(now), reminders_streak=0)
        turn = self._turn(user_id, now)
        # The user mentioning the answer of a card that is due but not being asked postpones it.
        self._postpone_leaks(turn, text, exclude=st["active_card_id"])
        event = "The user just wrote the last message in the conversation above. Reply to it."
        answering = bool(st["active_card_id"] and self.store.card(st["active_card_id"]))
        if answering:
            event += (
                f"\n\nThe ACTIVE CARD #{st['active_card_id']} is waiting for an answer. If this message "
                "answers it (even partly, or says they don't know), call grade_card before you reply. "
                "If the message reveals or asks for its answer, call postpone_card. Otherwise call "
                "not_an_answer, then just chat."
            )
        if st["editing_proposal_id"]:
            event += (
                f"\n\n(The user is editing card preview #{st['editing_proposal_id']}; "
                "if this message describes changes, call revise_proposal.)"
            )
        return await self._run(turn, event, must_use_tool=answering)

    async def ask_next(self, user_id: int, now: datetime | None = None) -> list[Action]:
        """Called by the timer when it's time for the next card."""
        now = now or datetime.now(timezone.utc)
        turn = self._turn(user_id, now)
        card = self._activate_next(turn)
        if card is None:
            return []
        event = (
            "It's time to bring up the active card. Ask about it naturally, as part of the "
            "conversation, without revealing the answer."
        )
        return await self._run(turn, event)

    async def remind(self, user_id: int, now: datetime | None = None) -> list[Action]:
        now = now or datetime.now(timezone.utc)
        st = self.store.state(user_id)
        turn = self._turn(user_id, now)
        level = pacing.annoyance_level(st["reminders_streak"])
        asked = parse(st["asked_at"])
        waited = srs.describe_interval(now, asked) if asked else "a while"
        event = (
            f"The user hasn't answered the active card for {waited}. Write reminder number "
            f"{st['reminders_streak'] + 1} today, tone: {level}. One or two sentences."
        )
        actions = await self._run(turn, event)
        self.store.update_state(
            user_id,
            reminders_streak=st["reminders_streak"] + 1,
            reminders_today=st["reminders_today"] + 1,
            last_reminder_at=iso(now),
        )
        return actions

    # ================= turn plumbing =================

    def _turn(self, user_id: int, now: datetime) -> Turn:
        user = dict(self.store.get_user(user_id))
        return Turn(user=user, now=now)

    async def _run(self, turn: Turn, event: str, must_use_tool: bool = False) -> list[Action]:
        uid = turn.user["id"]
        prompt = self._context(turn) + "\n\n<event>\n" + event + "\n</event>"
        llm = self.models.for_user(turn.user) if self.models else self.llm
        tools = self._tools(turn)
        if must_use_tool:
            # Gives a model that must call a tool an honest choice when the message isn't an answer.
            async def not_an_answer(args):
                return "OK. Don't grade; reply to the message. The card stays open."

            tools.append(
                ToolSpec(
                    "not_an_answer",
                    "Call this when the user's message is not an answer to the active card, so it stays open.",
                    _jsonschema({}, []),
                    not_an_answer,
                )
            )
        reply = strip_emoji(await llm.run(SYSTEM_PROMPT, prompt, tools, must_use_tool=must_use_tool))
        if reply:
            self._postpone_leaks(turn, reply, exclude=None)
            # The reply normally comes first. When it already asks the next card,
            # show the grade first so it isn't read as the grade of the new question.
            pos = sum(isinstance(a, RatingNote) for a in turn.actions) if turn.continued else 0
            turn.actions.insert(pos, Text(reply))
            self.store.log_message(uid, "bot", reply)
            self.store.update_state(uid, last_bot_at=iso(turn.now))
        return turn.actions

    def _postpone_leaks(self, turn: Turn, text: str, exclude: int | None) -> None:
        """Postpone due cards whose answer appears in the text.

        `exclude` is the card the user is currently answering (their answer is
        allowed to contain its answer)."""
        uid = turn.user["id"]
        st = self.store.state(uid)
        candidates = srs.due_queue(self.store, turn.user, turn.now)
        active = st["active_card_id"]
        if active and all(c["id"] != active for c in candidates):
            row = self.store.card(active)
            if row:
                candidates.append(dict(row))
        for c in candidates:
            if c["id"] == exclude or c["id"] in turn.graded or c["id"] in turn.postponed:
                continue
            note = self.store.note(c["note_id"])
            deck = self.store.deck(note["deck_id"])
            key = answer_key(deck_fields(deck), note_fields(note["fields"]), c["ord"])
            if contains_answer(text, key):
                self._postpone(turn, c["id"], "answer came up in conversation")

    def _postpone(self, turn: Turn, card_id: int, reason: str) -> None:
        uid = turn.user["id"]
        self.store.bury(card_id, srs.next_day_start(turn.user, turn.now))
        turn.postponed.add(card_id)
        st = self.store.state(uid)
        if st["active_card_id"] == card_id:
            self.store.update_state(uid, active_card_id=None, asked_at=None)
        self.store.log_message(uid, "note", f"card #{card_id} postponed to tomorrow ({reason})")
        log.info("postponed card %s: %s", card_id, reason)

    def _activate_next(self, turn: Turn) -> dict[str, Any] | None:
        uid = turn.user["id"]
        queue = srs.due_queue(self.store, turn.user, turn.now)
        queue = [c for c in queue if c["id"] not in turn.postponed and c["id"] not in turn.graded]
        if not queue:
            return None
        card = queue[0]
        self.store.update_state(
            uid, active_card_id=card["id"], asked_at=iso(turn.now), reminders_streak=0
        )
        return card

    def _timer_line(self, turn: Turn, st: Any, active: bool, queue: list[dict[str, Any]]) -> str:
        """What the code will do next on its own, so Claude never has to guess or promise."""
        u, now = turn.user, turn.now
        fmt = lambda t: t.astimezone(srs.tz_of(u)).strftime("%H:%M")  # noqa: E731
        if active:
            asked = parse(st["asked_at"])
            if not asked or st["reminders_today"] >= u["max_reminders"]:
                return "TIMER: waiting for the answer; no more reminders today."
            at = pacing.next_reminder_at(
                last_user_at=parse(st["last_user_at"]), asked_at=asked,
                streak=st["reminders_streak"], first_gap_min=u["first_reminder_min"],
            )
            return f"TIMER: waiting for the answer; a reminder goes out around {fmt(at)} if there is none."
        if not queue:
            later = srs.next_learning_due(self.store, u, now)
            if later and later < srs.next_day_start(u, now):
                return f"TIMER: nothing to ask right now; the next card comes back around {fmt(later)}."
            return "TIMER: nothing left to ask today."
        nxt = parse(st["next_ask_at"])
        at = max(nxt, now) if nxt else now
        if srs.is_quiet(u, at):
            return f"TIMER: next card at {fmt(at)} is in quiet hours, so only if the user is chatting then."
        return f"TIMER: the code brings up the next card at about {fmt(at)}."

    def _end_session(self, turn: Turn) -> timedelta:
        uid = turn.user["id"]
        remaining = len(srs.due_today(self.store, turn.user, turn.now))
        gap = pacing.gap_until_next_session(
            turn.now, srs.awake_end(turn.user, turn.now), remaining, turn.user["cards_per_session"]
        )
        self.store.update_state(
            uid,
            session_count=0,
            burst=0,
            next_ask_at=iso(turn.now + gap),
            active_card_id=None,
            asked_at=None,
        )
        return gap

    # ================= context =================

    def _card_brief(self, card: dict[str, Any], with_answer: bool) -> str:
        note = self.store.note(card["note_id"])
        deck = self.store.deck(note["deck_id"])
        fields = deck_fields(deck)
        values = note_fields(note["fields"])
        prompt, answer = card_sides(fields, values, card["ord"])
        direction = "reverse (answer -> prompt)" if card["ord"] == 1 else "forward"
        status = "NEW (never studied)" if card["state"] == srs.NEW else "review"
        s = f"#{card['id']} in deck \"{deck['name']}\" ({deck['deck_type']}, {direction}, {status})\n"
        s += f"  Ask about: {prompt}"
        if with_answer:
            s += f"\n  Answer (secret until they reply): {answer}"
        return s

    def _context(self, turn: Turn) -> str:
        u = turn.user
        uid = u["id"]
        now = turn.now
        tz = srs.tz_of(u)
        local = now.astimezone(tz)
        st = self.store.state(uid)
        quiet = srs.is_quiet(u, now)
        queue = srs.due_queue(self.store, u, now)
        later = srs.coming_back_today(self.store, u, now)

        lines = [
            "<context>",
            f"Now: {local:%A %Y-%m-%d %H:%M} ({u['timezone']}). Quiet hours {u['quiet_start']}-{u['quiet_end']}"
            + (" (it is quiet hours now; the user chose to be here)" if quiet else "") + ".",
            f"Settings: cards per session {u['cards_per_session']}, max reminders/day {u['max_reminders']}, "
            f"first reminder after {u['first_reminder_min']} min, desired retention {u['desired_retention']}.",
        ]
        decks = self.store.decks(uid)
        if decks:
            lines.append("Decks:")
            for d in decks:
                due_n = sum(1 for c in queue + later if c["deck_id"] == d["id"])
                lines.append(
                    f"- \"{d['name']}\" (id {d['id']}, {d['deck_type']}, fields: {', '.join(deck_fields(d))}; "
                    f"reverse {'on' if d['reverse'] else 'off'}; limits {d['new_per_day']} new / "
                    f"{d['reviews_per_day']} reviews a day; language {d['language'] or 'n/a'}): "
                    f"{self.store.count_notes(d['id'])} notes, {due_n} due today"
                )
        else:
            lines.append("Decks: none yet. Suggest creating one.")
        types = ", ".join(f"{k} ({v['hint']})" for k, v in DECK_TYPES.items())
        lines.append(f"Deck types available: {types}.")

        active = st["active_card_id"]
        if active and self.store.card(active):
            lines.append("ACTIVE CARD (asked, waiting for the user's answer):")
            lines.append(self._card_brief(dict(self.store.card(active)), with_answer=True))
        else:
            lines.append("ACTIVE CARD: none. Do not quiz the user on your own; call next_card if they want one.")

        lines.append(self._timer_line(turn, st, bool(active and self.store.card(active)), queue))

        others = [c for c in queue if c["id"] != active]
        if others:
            lines.append(
                f"Other cards due today ({len(others)}; don't reveal or discuss their answers; "
                "if the talk turns to one of them, call postpone_card):"
            )
            for c in others[:DUE_LIST_LIMIT]:
                note = self.store.note(c["note_id"])
                deck = self.store.deck(note["deck_id"])
                prompt, _ = card_sides(deck_fields(deck), note_fields(note["fields"]), c["ord"])
                lines.append(f"  #{c['id']}: {prompt}")
        if later:
            lines.append(
                f"Coming back later today ({len(later)}; learning steps after a recent answer, "
                "not askable yet; the code brings them up when due):"
            )
            for c in later[:DUE_LIST_LIMIT]:
                note = self.store.note(c["note_id"])
                deck = self.store.deck(note["deck_id"])
                prompt, _ = card_sides(deck_fields(deck), note_fields(note["fields"]), c["ord"])
                at = parse(c["due"]).astimezone(tz).strftime("%H:%M")
                lines.append(f"  #{c['id']}: {prompt} (at {at})")
        ahead = srs.upcoming(self.store, u, now)
        if ahead:
            parts = ", ".join(f"{d.astimezone(tz):%a %d %b} {n}" for d, n in ahead)
            lines.append(f"Reviews already scheduled for the next days (not counting new cards): {parts}.")
        else:
            lines.append("Reviews already scheduled for the next 7 days: none (new cards not counted).")
        if st["editing_proposal_id"]:
            p = self.store.proposal(st["editing_proposal_id"])
            if p and p["status"] == "pending":
                lines.append(f"Card preview #{p['id']} being edited, current fields: {p['fields']}")
        lines.append("</context>")

        convo = ["<conversation>"]
        recent = self.store.recent_messages(uid, HISTORY_MESSAGES)
        for i, m in enumerate(recent):
            ts = parse(m["created_at"]).astimezone(tz).strftime("%H:%M")
            who = {"user": "User", "bot": "You", "note": "[system]"}[m["role"]]
            text = m["text"]
            # The newest message stays whole; older long ones are shortened.
            if i < len(recent) - 1 and len(text) > MAX_MESSAGE_CHARS:
                text = text[:MAX_MESSAGE_CHARS] + " [...]"
            convo.append(f"[{ts}] {who}: {text}")
        convo.append("</conversation>")
        return "\n".join(lines) + "\n\n" + "\n".join(convo)

    # ================= tools =================

    def _resolve_deck(self, uid: int, ref: Any):
        if ref is None or ref == "":
            decks = self.store.decks(uid)
            if len(decks) == 1:
                return decks[0]
            raise ValueError("Which deck? Pass the deck name.")
        s = str(ref).strip()
        if s.isdigit():
            d = self.store.deck(int(s))
            if d and d["user_id"] == uid:
                return d
        d = self.store.deck_by_name(uid, s)
        if d:
            return d
        names = ", ".join(x["name"] for x in self.store.decks(uid)) or "none"
        raise ValueError(f"No deck called {s!r}. Existing decks: {names}")

    def _owned_card(self, uid: int, card_id: Any):
        row = self.store.card(int(card_id))
        if row is None or row["user_id"] != uid:
            raise ValueError(f"No card #{card_id}")
        return row

    def _tools(self, turn: Turn) -> list[ToolSpec]:
        store = self.store
        uid = turn.user["id"]

        async def list_decks(args):
            decks = store.decks(uid)
            if not decks:
                return "No decks yet."
            return "\n".join(
                f"{d['name']} (id {d['id']}, {d['deck_type']}, {store.count_notes(d['id'])} notes)" for d in decks
            )

        async def create_deck(args):
            name = args["name"].strip()
            dtype = args.get("deck_type", "vocabulary")
            if dtype not in DECK_TYPES:
                raise ValueError(f"deck_type must be one of {list(DECK_TYPES)}")
            if store.deck_by_name(uid, name):
                return f"A deck called {name!r} already exists."
            fields = args.get("fields") or DECK_TYPES[dtype]["fields"]
            if len(fields) < 2:
                raise ValueError("A deck needs at least two fields (prompt and answer).")
            deck_id = store.create_deck(
                uid, name, dtype, fields, bool(args.get("reverse", False)), args.get("language")
            )
            store.log_message(uid, "note", f"deck {name!r} created")
            return f"Created deck {name!r} (id {deck_id}) with fields {fields}."

        async def update_deck(args):
            d = self._resolve_deck(uid, args["deck"])
            changes: dict[str, Any] = {}
            if args.get("new_name"):
                if store.deck_by_name(uid, args["new_name"]):
                    raise ValueError("Another deck already has that name.")
                changes["name"] = args["new_name"].strip()
            if args.get("fields"):
                old = deck_fields(d)
                new = [f.strip() for f in args["fields"] if f.strip()]
                if len(new) < 2:
                    raise ValueError("A deck needs at least two fields.")
                changes["fields"] = new
                # Carry values over: fields keep their position, renamed ones keep their value.
                for n in store.notes_in_deck(d["id"]):
                    vals = note_fields(n["fields"])
                    moved = {}
                    for i, f in enumerate(new):
                        if f in vals:
                            moved[f] = vals[f]
                        elif i < len(old) and old[i] not in new:
                            moved[f] = vals.get(old[i], "")
                        else:
                            moved[f] = ""
                    store.update_note(n["id"], moved, sort_key(new, moved))
            if "reverse" in args and args["reverse"] is not None:
                changes["reverse"] = int(bool(args["reverse"]))
                if args["reverse"] and not d["reverse"]:
                    store.add_reverse_cards(d["id"])
            for k in ("new_per_day", "reviews_per_day"):
                if args.get(k) is not None:
                    changes[k] = max(0, int(args[k]))
            if args.get("language"):
                changes["language"] = args["language"]
            if not changes:
                return "Nothing to change."
            store.update_deck(d["id"], **changes)
            return f"Deck updated: {changes}"

        async def search_cards(args):
            deck_id = self._resolve_deck(uid, args["deck"])["id"] if args.get("deck") else None
            rows = store.search_notes(uid, args["query"], deck_id)
            if not rows:
                return "No matching cards."
            out = []
            for r in rows:
                cards = store.q("SELECT * FROM cards WHERE note_id=? ORDER BY ord", (r["id"],))
                when = "; ".join(srs.comes_back(turn.user, c, turn.now) for c in cards)
                out.append(f"note #{r['id']} in {r['deck_name']}: {r['fields']} (comes back: {when})")
            return "\n".join(out)

        async def propose_card(args):
            d = self._resolve_deck(uid, args["deck"])
            fields = deck_fields(d)
            values = clean_fields(fields, args.get("fields") or {})
            if not values[fields[0]] or not values[fields[1]]:
                raise ValueError(f"Fill at least {fields[0]!r} and {fields[1]!r}.")
            key = sort_key(fields, values)
            dups = store.notes_by_key(d["id"], key)
            if dups and not args.get("allow_duplicate"):
                return (
                    f"Duplicate: deck {d['name']!r} already has note #{dups[0]['id']}: {dups[0]['fields']}. "
                    "Tell the user instead of adding. Only if they insist, call again with allow_duplicate=true."
                )
            similar = [
                r for r in store.search_notes(uid, values[fields[0]], d["id"], limit=5) if r["sort_key"] != key
            ]
            # A new version of a card that is still waiting for Add/Skip replaces the old preview.
            old = [
                p for p in store.pending_proposals(uid)
                if p["deck_id"] == d["id"] and sort_key(fields, note_fields(p["fields"])) == key
            ]
            for p in old:
                store.update_proposal(p["id"], status="replaced")
            store.update_state(uid, editing_proposal_id=None)
            pid = store.create_proposal(uid, d["id"], values)
            turn.actions.append(Preview(pid, replaces=tuple(p["message_id"] for p in old if p["message_id"])))
            msg = f"Preview #{pid} shown to the user with Add / Edit / Skip buttons."
            if similar:
                msg += " Similar existing notes (mention if relevant): " + "; ".join(r["fields"] for r in similar)
            return msg

        async def revise_proposal(args):
            p = store.proposal(int(args["proposal_id"]))
            if p is None or p["user_id"] != uid or p["status"] != "pending":
                raise ValueError("That preview is no longer open.")
            d = store.deck(p["deck_id"])
            fields = deck_fields(d)
            values = clean_fields(fields, {**note_fields(p["fields"]), **(args.get("fields") or {})})
            store.update_proposal(p["id"], fields=values)
            store.update_state(uid, editing_proposal_id=None)
            turn.actions.append(Preview(p["id"], replaces=(p["message_id"],) if p["message_id"] else ()))
            return f"Preview #{p['id']} updated; the old version was removed and the user can now tap Add."

        async def edit_card(args):
            note = store.note(int(args["note_id"]))
            if note is None or store.deck(note["deck_id"])["user_id"] != uid:
                raise ValueError("No such note.")
            fields = deck_fields(store.deck(note["deck_id"]))
            values = clean_fields(fields, {**note_fields(note["fields"]), **(args.get("fields") or {})})
            store.update_note(note["id"], values, sort_key(fields, values))
            return f"Note #{note['id']} updated: {values}"

        async def delete_card(args):
            note = store.note(int(args["note_id"]))
            if note is None or store.deck(note["deck_id"])["user_id"] != uid:
                raise ValueError("No such note.")
            turn.actions.append(ConfirmDelete(note["id"]))
            return "The user was asked to confirm the deletion with a button."

        async def grade_card(args):
            st = store.state(uid)
            card_id = int(args["card_id"])
            if st["active_card_id"] != card_id:
                raise ValueError(f"Card #{card_id} is not the active card; only the active card can be graded.")
            rating = srs.RATING_BY_NAME.get(str(args["rating"]).lower())
            if rating is None:
                raise ValueError("rating must be Again, Hard, Good or Easy")
            log_id = srs.grade(store, turn.user, card_id, rating, "claude", args.get("reason"), turn.now)
            tz = srs.tz_of(turn.user)
            back = (
                f"(Only if the user asks: this card comes back {srs.comes_back(turn.user, store.card(card_id), turn.now)}.)"
            )
            turn.graded.add(card_id)
            turn.actions.append(RatingNote(log_id))
            store.log_message(uid, "note", f"card #{card_id} graded {srs.RATING_NAMES[rating]}")
            count = st["session_count"] + 1
            store.update_state(uid, active_card_id=None, asked_at=None, session_count=count, reminders_streak=0)
            target = turn.user["cards_per_session"] + st["burst"]
            if count < target:
                nxt = self._activate_next(turn)
                if nxt is not None:
                    turn.continued = True
                    return (
                        f"Graded {srs.RATING_NAMES[rating]}. {back} Continue the session: ask this next card now, "
                        f"in the same message, without revealing its answer:\n" + self._card_brief(nxt, True)
                    )
            gap = self._end_session(turn)
            left = len(srs.due_today(store, turn.user, turn.now))
            if left:
                at = (turn.now + gap).astimezone(tz).strftime("%H:%M")
                return (
                    f"Graded {srs.RATING_NAMES[rating]}. {back} Session done: do NOT ask another card now. "
                    f"{left} cards left today; the code brings up the next one at about {at}. "
                    "Don't mention timing unless the user asks; if they do, give that exact time."
                )
            return f"Graded {srs.RATING_NAMES[rating]}. {back} That was the last card due for now. Do not ask another."

        async def postpone_card(args):
            card_id = int(args["card_id"])
            self._owned_card(uid, card_id)
            self._postpone(turn, card_id, args.get("reason") or "postponed by Claude")
            return f"Card #{card_id} postponed to tomorrow, not graded."

        async def next_card(args):
            st = store.state(uid)
            want = max(1, int(args.get("count") or 1))
            if st["active_card_id"]:
                store.update_state(uid, burst=st["burst"] + want - 1)
                return "A card is already active; ask it:\n" + self._card_brief(
                    dict(store.card(st["active_card_id"])), True
                )
            nxt = parse(st["next_ask_at"])
            if nxt and nxt > turn.now and not args.get("user_asked_now"):
                local = nxt.astimezone(srs.tz_of(turn.user)).strftime("%H:%M")
                return (
                    f"Not opened: the next card is scheduled for {local} and the code will ask it then. "
                    "Only if the user clearly asked to review right now, call next_card again with "
                    "user_asked_now=true. Otherwise tell them it comes at " + local + "."
                )
            store.update_state(
                uid, session_count=0, burst=max(0, want - turn.user["cards_per_session"])
            )
            card = self._activate_next(turn)
            if card is None:
                return "Nothing is due right now. Tell the user they're all caught up (or offer to add new cards)."
            return "Ask this card now, without revealing the answer:\n" + self._card_brief(card, True)

        async def set_next_card_time(args):
            minutes = min(720, max(0, int(args["minutes"])))
            st = store.state(uid)
            when = turn.now + timedelta(minutes=minutes)
            changes: dict[str, Any] = {"next_ask_at": iso(when), "session_count": 0, "burst": 0}
            if st["active_card_id"]:
                # The open question is withdrawn; the card stays due and comes up at the new time.
                changes.update(active_card_id=None, asked_at=None)
            store.update_state(uid, **changes)
            local = when.astimezone(srs.tz_of(turn.user)).strftime("%H:%M")
            msg = f"The code will bring up the next card at about {local} (within a minute)."
            if st["active_card_id"]:
                msg += " The open question was withdrawn; don't wait for an answer to it now."
            if srs.is_quiet(turn.user, when):
                msg += " That's in quiet hours, so it only happens if the user is chatting then."
            return msg

        async def update_settings(args):
            changes: dict[str, Any] = {}
            if args.get("timezone"):
                tzname = args["timezone"]
                if tzname not in available_timezones():
                    raise ValueError("Use an IANA timezone name like Europe/Berlin.")
                ZoneInfo(tzname)
                changes["timezone"] = tzname
            for k in ("quiet_start", "quiet_end"):
                if args.get(k):
                    h, m = str(args[k]).split(":")
                    changes[k] = f"{int(h) % 24:02d}:{int(m):02d}"
            for k, lo, hi in (("cards_per_session", 1, 50), ("max_reminders", 0, 20), ("first_reminder_min", 10, 1440)):
                if args.get(k) is not None:
                    changes[k] = min(hi, max(lo, int(args[k])))
            if args.get("desired_retention") is not None:
                changes["desired_retention"] = min(0.99, max(0.7, float(args["desired_retention"])))
            if not changes:
                return "Nothing to change."
            store.update_user(uid, **changes)
            turn.user.update(changes)
            return f"Settings updated: {changes}"

        deck_ref = {"type": "string", "description": "Deck name (or id)"}
        fields_obj = {"type": "object", "additionalProperties": {"type": "string"},
                      "description": "Field name -> value, using the deck's fields"}
        return [
            ToolSpec("list_decks", "List the user's decks.", _jsonschema({}, []), list_decks),
            ToolSpec(
                "create_deck",
                "Create a deck. deck_type picks the template; fields overrides the template's field list.",
                _jsonschema(
                    {
                        "name": STR,
                        "deck_type": {"type": "string", "enum": list(DECK_TYPES)},
                        "language": {"type": "string", "description": "Language being learned, if any"},
                        "reverse": {"type": "boolean", "description": "Also make answer->prompt cards"},
                        "fields": {"type": "array", "items": STR},
                    },
                    ["name", "deck_type"],
                ),
                create_deck,
            ),
            ToolSpec(
                "update_deck",
                "Rename a deck, change its template fields, turn reverse cards on/off, or change daily limits.",
                _jsonschema(
                    {
                        "deck": deck_ref,
                        "new_name": STR,
                        "fields": {"type": "array", "items": STR},
                        "reverse": {"type": "boolean"},
                        "new_per_day": INT,
                        "reviews_per_day": INT,
                        "language": STR,
                    },
                    ["deck"],
                ),
                update_deck,
            ),
            ToolSpec(
                "search_cards",
                "Search the user's notes by text. Returns note ids and fields.",
                _jsonschema({"query": STR, "deck": deck_ref}, ["query"]),
                search_cards,
            ),
            ToolSpec(
                "propose_card",
                "Check for duplicates and show the user a preview of a new note with Add/Edit/Skip buttons.",
                _jsonschema(
                    {"deck": deck_ref, "fields": fields_obj, "allow_duplicate": {"type": "boolean"}},
                    ["deck", "fields"],
                ),
                propose_card,
            ),
            ToolSpec(
                "revise_proposal",
                "Change the fields of a pending card preview; the preview message updates in place.",
                _jsonschema({"proposal_id": INT, "fields": fields_obj}, ["proposal_id", "fields"]),
                revise_proposal,
            ),
            ToolSpec(
                "edit_card",
                "Change fields of an existing note (fix a typo, improve an example).",
                _jsonschema({"note_id": INT, "fields": fields_obj}, ["note_id", "fields"]),
                edit_card,
            ),
            ToolSpec(
                "delete_card",
                "Ask the user to confirm deleting a note (and its cards).",
                _jsonschema({"note_id": INT}, ["note_id"]),
                delete_card,
            ),
            ToolSpec(
                "grade_card",
                "Grade the user's answer to the ACTIVE card. Call once per answer.",
                _jsonschema(
                    {
                        "card_id": INT,
                        "rating": {"type": "string", "enum": ["Again", "Hard", "Good", "Easy"]},
                        "reason": {
                            "type": "string",
                            "description": "One short line shown to the user: why this grade. Never mention "
                            "when the card comes back; the code adds the real time.",
                        },
                    },
                    ["card_id", "rating", "reason"],
                ),
                grade_card,
            ),
            ToolSpec(
                "postpone_card",
                "Postpone a due card to tomorrow without grading it, because its answer came up before it was asked.",
                _jsonschema({"card_id": INT, "reason": STR}, ["card_id"]),
                postpone_card,
            ),
            ToolSpec(
                "next_card",
                "The user explicitly asked to review now. Opens the next due card (count = how many in a row "
                "they want). If a card is scheduled for later, set user_asked_now=true only when they "
                "really asked for one now, not 'when the timer is up'.",
                _jsonschema({"count": INT, "user_asked_now": {"type": "boolean"}}, []),
                next_card,
            ),
            ToolSpec(
                "set_next_card_time",
                "The user wants the next card at a specific time ('in 10 minutes', 'at 23:00'). "
                "minutes = minutes from now. Withdraws the open question, if any, until then.",
                _jsonschema({"minutes": INT}, ["minutes"]),
                set_next_card_time,
            ),
            ToolSpec(
                "update_settings",
                "Change user settings: timezone (IANA), quiet hours (HH:MM), cards per session, reminders, desired retention.",
                _jsonschema(
                    {
                        "timezone": STR,
                        "quiet_start": STR,
                        "quiet_end": STR,
                        "cards_per_session": INT,
                        "max_reminders": INT,
                        "first_reminder_min": INT,
                        "desired_retention": {"type": "number"},
                    },
                    [],
                ),
                update_settings,
            ),
        ]

    # ================= helpers used by the Telegram layer =================

    def add_proposal(self, proposal_id: int, user_id: int) -> str:
        """User tapped Add on a preview. Returns a status line."""
        p = self.store.proposal(proposal_id)
        if p is None or p["user_id"] != user_id:
            return "That preview is gone."
        if p["status"] != "pending":
            return f"Already {p['status']}."
        d = self.store.deck(p["deck_id"])
        if d is None:
            return "The deck no longer exists."
        fields = deck_fields(d)
        values = note_fields(p["fields"])
        key = sort_key(fields, values)
        note_id = self.store.add_note(d["id"], values, key, bool(d["reverse"]))
        self.store.update_proposal(proposal_id, status="added", note_id=note_id)
        self.store.update_state(user_id, editing_proposal_id=None)
        self.store.log_message(user_id, "note", f"user added {values.get(fields[0])!r} to {d['name']!r}")
        return f"Added to {d['name']}"

    def skip_proposal(self, proposal_id: int, user_id: int) -> str:
        p = self.store.proposal(proposal_id)
        if p is None or p["user_id"] != user_id or p["status"] != "pending":
            return "That preview is gone."
        self.store.update_proposal(proposal_id, status="skipped")
        self.store.update_state(user_id, editing_proposal_id=None)
        self.store.log_message(user_id, "note", f"user skipped preview #{proposal_id}")
        return "Skipped."

    def start_edit(self, proposal_id: int, user_id: int) -> None:
        self.store.update_state(user_id, editing_proposal_id=proposal_id)


def proposal_payload(store: Store, proposal_id: int) -> tuple[list[str], dict[str, str], str]:
    p = store.proposal(proposal_id)
    d = store.deck(p["deck_id"])
    return deck_fields(d), note_fields(p["fields"]), d["name"]

