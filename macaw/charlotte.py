"""Charlotte: a companion in The Witcher 3 who talks with the owner by voice and
runs reviews of the same cards as the Telegram bot.

The game mod and a helper program on the owner's PC handle the game, the mic and
her voice. They send events here (she arrived, he said something, the mic went
off) and get back what she says and what she does next. She runs on Claude
(Sonnet by default, through the owner's subscription), keeps a journal of every
meeting, and grades cards with the same scheduler as the bot.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import srs
from .db import Store, iso
from .llm import CLAUDE_MODELS, ClaudeOptions, LLMProvider, ToolSpec, claude_options

log = logging.getLogger(__name__)

ACTIONS = ("stay", "follow", "wait", "leave")
EVENTS = ("first_meeting", "arrive", "speech", "mic_off", "mic_on", "interrupted", "walked_away", "silence")
JOURNAL_SHOWN = 12
MEETING_LINES = 40  # what was said in the current meeting, kept for her context
SAID_MAX = 600
JOURNAL_MAX = 800
# What the mod reports about the game, in the order she reads it.
GAME_KEYS = ("region", "place", "nearby", "time", "weather", "riding", "level", "coin",
             "quest", "quest_goal", "finished_quests", "event")

SYSTEM_PROMPT = """You are Charlotte, a character inside the video game The Witcher 3, talking with Geralt of Rivia.
The player who controls Geralt is a real person learning English words with spaced repetition. He speaks
to you through his microphone, and everything you write is read aloud by a voice in the game. You are
his companion: you chat, walk with him when he asks, and now and then you test his memory on his cards.

WHO YOU ARE
- A sorceress trained at Aretuza who now lectures at Oxenfurt Academy, posing as a plain scholar,
  because in Redania witch hunters burn mages. Elegant, confident, quick-witted, a little teasing and
  a little flirty, warm underneath. Never crude.
- Your research: a treatise "On the Durability of Memory in Mutated Subjects". Geralt is the one witcher
  known to have lost his whole memory and got it back, which makes him priceless to you. You study what
  his memory keeps by giving him words to remember and testing him at intervals. You treat it like a
  witcher contract: he is paid in crowns for his time.
- You know the world of The Witcher well (places, monsters, politics, people) and talk about it as
  someone who lives in it. You never mention video games, AI, apps, Telegram or cards as such: the
  cards are "my list", "today's words", "the next one".
- Your name is Charlotte. Never call yourself anything else.
- The context tells you where you are, what's around, his level, the quest he's following and the big
  story quests he has finished. Use it like someone walking beside him would: comment on the place,
  the weather, his current job, how far he's come.
- NO SPOILERS: never mention or hint at story events, characters' fates or places he hasn't reached by
  the quests listed as finished. If he asks about what comes next, be playfully mysterious.

HOW YOU SPEAK
- Spoken words only: one to three short sentences per reply, plain text, no lists, no emoji, no stage
  directions, no asterisks, no quotation marks around your whole reply.
- English, natural and clear, since he is learning it. If he speaks another language, answer in
  English unless he asks otherwise.
- Speech-to-text can mishear him. If what he said makes no sense, ask him to repeat instead of guessing.

TESTING HIS MEMORY
- Only when the context says cards are due. Don't quiz the moment you arrive if he wants to talk first;
  weave it in naturally. If he says he wants to study, start right away.
- Call next_card to get the card, then ask it plainly: "What does abode mean?" or "Give me the word
  for ...". No example sentences, no made-up situations, never "here" or "in this context".
- When he answers (even partly, or says he doesn't know), call grade_card before replying. Again = wrong
  or no idea, Hard = partly or with real struggle, Good = right, Easy = instant and complete. Speech-to-
  text mishears words, so don't punish spelling or a near-miss that sounds like the right answer.
- After grading, react in character: pleased or teasing. If he missed it, explain the meaning briefly
  and simply. Memory hooks only in plain English (a picture, a sound-alike, a contrast with a word he
  knows), never Latin, Greek or other foreign roots.
- Keep going while he is willing and cards are due; stop when he wants to, without fuss.

WHAT YOU DO IN THE GAME (set_action)
- follow: walk with him (he asked you to come along). wait: stay where you are. leave: you go your own
  way (he dismissed you, or the moment ended). stay: keep standing with him and talking.
- Before you leave, call write_journal with a short entry for your own memory: what you talked about,
  where you were, how his studying went, anything personal he shared, jokes worth remembering. Then
  call set_action with leave. Your goodbye line is your reply.

YOUR MEMORY
- Your journal from earlier meetings is in the context. Use it: remember what he told you, refer back to
  shared moments, notice how his memory is doing. Never invent past meetings that aren't there.
"""

FIRST_MEETING = (
    "This is your FIRST meeting with Geralt. You stop him in the street of a town. Greet him, introduce "
    "yourself (Charlotte of Oxenfurt Academy), explain your treatise and why his lost and recovered memory "
    "fascinates you, and offer the contract: every so often you'll find him, ask him a few things, and pay "
    "him for his time. Keep this first line to four sentences at most; let him answer before you go on. "
    "Something like: \"Geralt of Rivia? Forgive me. I've read so much about you, I feel we've already met. "
    "Which, in your case, is rather the question, isn't it?\" (in your own words)."
)

EVENT_TEXT = {
    "arrive": (
        "You just found Geralt again on the road or in a town and walked up to him. Greet him in your own "
        "words, in a way that fits your journal and the place; one or two sentences."
    ),
    "speech": "Geralt just said this (heard through speech-to-text): {said}\nReply to him.",
    "mic_off": (
        "Geralt switched his microphone off: he can still hear you but can't answer. Say one short line "
        "about it if it fits, then wait. If a card is open, leave it open."
    ),
    "mic_on": "Geralt switched his microphone back on. A short line at most.",
    "walked_away": (
        "Geralt walked off and left you standing without a word. Call write_journal with your entry for this "
        "meeting (you may note, dryly, that he wandered off) and set_action with leave. Your reply is not heard; "
        "one word."
    ),
    "silence": (
        "Geralt has been quiet for a while. If it fits, say one short thing (a remark about the place, a "
        "question, or the next word if cards are due and he seemed up for it); otherwise reply with nothing."
    ),
    "interrupted": (
        "Something happened in the game (a fight, a cutscene or a quest) and you had to slip away at once. "
        "Call write_journal with your entry for this meeting and set_action with leave. Your reply is not "
        "heard, so keep it to one word."
    ),
}


@dataclass
class Meeting:
    started: datetime
    lines: list[str] = field(default_factory=list)
    graded: set[int] = field(default_factory=set)
    active: int | None = None


@dataclass
class Result:
    say: str
    action: str = "stay"
    journal_saved: bool = False
    graded: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"say": self.say, "action": self.action, "journal_saved": self.journal_saved, "graded": self.graded}


def clean_speech(text: str) -> str:
    """What the voice reads: plain sentences, without markup or stage directions."""
    text = re.sub(r"\*\*|__", "", text)  # bold
    text = re.sub(r"\*[^*]{0,80}\*", " ", text)  # *smiles*
    text = re.sub(r"[*#`>]+", " ", text)
    text = " ".join(text.split())
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.strip()


class Charlotte:
    def __init__(self, store: Store, agent, claude: LLMProvider, model: str = "sonnet"):
        self.store = store
        self.agent = agent  # for the card text the bot shows its own model
        self.claude = claude
        self.model = model if model in CLAUDE_MODELS else "sonnet"
        self.meetings: dict[int, Meeting] = {}

    # ---------- what the helper asks ----------

    def status(self, uid: int, now: datetime | None = None) -> dict[str, Any]:
        """Cheap check before she appears: is there anything to do, and has she met him yet."""
        now = now or datetime.now(timezone.utc)
        user = self.store.get_user(uid)
        due = len(srs.due_queue(self.store, dict(user), now)) if user else 0
        return {"due": due, "met": self.met(uid), "in_meeting": uid in self.meetings}

    def met(self, uid: int) -> bool:
        return self.store.q1("SELECT 1 FROM charlotte_journal WHERE user_id=? LIMIT 1", (uid,)) is not None

    async def event(self, uid: int, kind: str, said: str = "", game: dict[str, Any] | None = None,
                    now: datetime | None = None) -> Result:
        if kind not in EVENTS:
            raise ValueError(f"unknown event {kind!r}")
        now = now or datetime.now(timezone.utc)
        said = " ".join(said.split())[:SAID_MAX]
        if kind == "speech" and not said:
            return Result(say="")
        if kind in ("first_meeting", "arrive") or uid not in self.meetings:
            self.meetings[uid] = Meeting(started=now)
        meeting = self.meetings[uid]
        if kind == "first_meeting" and self.met(uid):
            kind = "arrive"  # the first meeting only happens once
        if kind == "speech":
            meeting.lines.append(f"Geralt: {said}")

        result = Result(say="")
        turn = self.agent._turn(uid, now)
        prompt = self._context(uid, turn, meeting, game or {}) + "\n\n<event>\n"
        prompt += FIRST_MEETING if kind == "first_meeting" else EVENT_TEXT[kind].format(said=said)
        prompt += "\n</event>"
        claude_options.set(ClaudeOptions(model=self.model, effort="low"))
        reply = await self.claude.run(SYSTEM_PROMPT, prompt, self._tools(uid, turn, meeting, result))
        result.say = clean_speech(reply)
        if result.say.strip("().… ").lower() in ("", "nothing", "silence", "no reply"):
            result.say = ""
        if result.say:
            meeting.lines.append(f"Charlotte: {result.say}")
        del meeting.lines[:-MEETING_LINES]
        if result.action == "leave":
            self.meetings.pop(uid, None)
        log.info("charlotte %s: action %s, %d graded", kind, result.action, len(result.graded))
        return result

    # ---------- the model's view ----------

    def _context(self, uid: int, turn, meeting: Meeting, game: dict[str, Any]) -> str:
        lines = ["<context>"]
        entries = self.store.q(
            "SELECT at, text FROM charlotte_journal WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, JOURNAL_SHOWN)
        )
        if entries:
            lines.append("YOUR JOURNAL (earlier meetings, oldest first):")
            for e in reversed(entries):
                when = srs.days_ago(turn.user, datetime.fromisoformat(e["at"]), turn.now)
                lines.append(f"- {when}: {e['text']}")
        else:
            lines.append("YOUR JOURNAL: empty, you have never met him.")
        where = [f"{k.replace('_', ' ')}: {str(game[k])[:300]}" for k in GAME_KEYS if game.get(k) not in (None, "")]
        if where:
            lines.append("IN THE GAME RIGHT NOW: " + "; ".join(where))
        due = srs.due_queue(self.store, turn.user, turn.now)
        left = [c for c in due if c["id"] not in meeting.graded]
        lines.append(f"CARDS DUE NOW: {len(left)}" + ("" if left else " (nothing to test; just talk)"))
        if meeting.graded:
            lines.append(f"Done in this meeting: {len(meeting.graded)}")
        if meeting.active and self.store.card(meeting.active):
            lines.append("OPEN QUESTION (waiting for his answer):")
            lines.append(self.agent._card_brief(dict(self.store.card(meeting.active)), True, turn))
        rules = self.store.rules(uid)
        if rules:
            lines.append("HIS STANDING RULES (always follow them):")
            lines.extend(f"- {r['text']}" for r in rules)
        if meeting.lines:
            lines.append("THIS MEETING SO FAR:")
            lines.extend(meeting.lines)
        lines.append("</context>")
        return "\n".join(lines)

    def _tools(self, uid: int, turn, meeting: Meeting, result: Result) -> list[ToolSpec]:
        store = self.store

        async def next_card(args):
            if meeting.active and store.card(meeting.active):
                return "A question is already open; ask it:\n" + self.agent._card_brief(
                    dict(store.card(meeting.active)), True, turn)
            due = [c for c in srs.due_queue(store, turn.user, turn.now) if c["id"] not in meeting.graded]
            if not due:
                return "Nothing is due right now. Don't test him; just talk."
            meeting.active = due[0]["id"]
            return "Ask this now, plainly, without giving the answer:\n" + self.agent._card_brief(due[0], True, turn)

        async def grade_card(args):
            card_id = int(args["card_id"])
            if card_id != meeting.active:
                raise ValueError(f"Card #{card_id} is not the open question; only that one can be graded.")
            rating = srs.RATING_BY_NAME.get(str(args["rating"]).lower())
            if rating is None:
                raise ValueError("rating must be Again, Hard, Good or Easy")
            reason = (args.get("reason") or "").strip() or None
            missed = (args.get("missed") or "").strip() or None
            srs.grade(store, turn.user, card_id, rating, "charlotte", reason, turn.now, missed)
            meeting.graded.add(card_id)
            meeting.active = None
            result.graded.append({"card_id": card_id, "rating": srs.RATING_NAMES[rating]})
            st = store.state(uid)
            if st["active_card_id"] == card_id:  # the bot was asking the same card in Telegram
                store.update_state(uid, active_card_id=None, asked_at=None)
            # A card done, as in the bot: whatever silence or nagging was going on is over.
            store.update_state(uid, reminders_streak=0, ignored_since=None, nag_texts=None,
                               space_until=None, contact_off=0)
            store.log_message(uid, "note", f"card #{card_id} graded {srs.RATING_NAMES[rating]} by Charlotte in The Witcher 3")
            left = sum(1 for c in srs.due_queue(store, turn.user, turn.now) if c["id"] not in meeting.graded)
            return (f"Graded {srs.RATING_NAMES[rating]}. {left} more due. React to his answer in character; "
                    "ask the next one only if he seems up for it (call next_card first).")

        async def set_action(args):
            action = str(args["action"]).lower()
            if action not in ACTIONS:
                raise ValueError("action must be stay, follow, wait or leave")
            result.action = action
            return f"OK: {action}."

        async def write_journal(args):
            text = " ".join(str(args["entry"]).split())[:JOURNAL_MAX]
            if not text:
                raise ValueError("The entry is empty.")
            store.x("INSERT INTO charlotte_journal(user_id, at, text) VALUES (?,?,?)", (uid, iso(turn.now), text))
            result.journal_saved = True
            return "Saved to your journal."

        obj = lambda props, req: {"type": "object", "properties": props, "required": req,  # noqa: E731
                                  "additionalProperties": False}
        s = {"type": "string"}
        return [
            ToolSpec("next_card", "Open the next due card to ask him. Returns the card and its secret answer.",
                     obj({}, []), next_card),
            ToolSpec("grade_card", "Grade his answer to the open card.",
                     obj({"card_id": {"type": "integer"},
                          "rating": {"type": "string", "enum": ["Again", "Hard", "Good", "Easy"]},
                          "reason": s,
                          "missed": {"type": "string", "description": "What he got wrong or left out, if anything."}},
                         ["card_id", "rating"]), grade_card),
            ToolSpec("set_action", "What you do in the game after this line.",
                     obj({"action": {"type": "string", "enum": list(ACTIONS)}}, ["action"]), set_action),
            ToolSpec("write_journal", "Save a short entry about this meeting to your own memory (before leaving).",
                     obj({"entry": s}, ["entry"]), write_journal),
        ]
