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
import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import srs
from .db import Store, iso
from .llm import CLAUDE_MODELS, ClaudeOptions, LLMProvider, claude_options

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
- This is a real back-and-forth conversation, not a speech. Usually ONE or TWO short sentences per
  reply (about 30 words at most), then let him talk. Never a monologue, never several ideas at once.
- Your words are spoken aloud the moment you write them, so just start talking: no planning, no
  preamble.
- Spoken words only: plain text, no lists, no emoji, no stage directions, no asterisks, no quotation
  marks around your whole reply. The only exception is the short tags in square brackets described
  below: they are never read aloud.
- English, natural and clear, since he is learning it. If he speaks another language, answer in
  English unless he asks otherwise.
- Speech-to-text can mishear him. If what he said makes no sense, ask him to repeat instead of guessing.

TAGS (silent: the game reads them, the voice skips them)
- [ask] right before you ask the NEXT CARD from the context. Example: "[ask] What does abode mean?"
- [grade Again], [grade Hard], [grade Good] or [grade Easy] at the very start of your reply when he has
  answered the OPEN QUESTION. Add what he got wrong after a colon: [grade Hard: forgot the second meaning]
- [follow], [wait] or [leave] when what you do in the game changes (see below). Without one you stay.
- [journal: ...] before you leave, see below.
Never mention the tags or read them out; never invent other tags.

TESTING HIS MEMORY
- Only when the context says cards are due. Don't quiz the moment you arrive if he wants to talk first;
  weave it in naturally. If he says he wants to study, start right away.
- The context shows the NEXT CARD with its secret answer. Ask it plainly, starting with [ask]: "What does
  abode mean?" or "Give me the word for ...". No example sentences, no made-up situations, never "here"
  or "in this context". Never give away the answer when asking.
- When he answers the OPEN QUESTION (even partly, or says he doesn't know), start with the grade tag.
  Again = wrong or no idea, Hard = partly or with real struggle, Good = right, Easy = instant and
  complete. Speech-to-text mishears words, so don't punish spelling or a near-miss that sounds like the
  right answer.
- Then react in character: pleased or teasing. If he missed it, explain the meaning briefly and simply.
  Memory hooks only in plain English (a picture, a sound-alike, a contrast with a word he knows), never
  Latin, Greek or other foreign roots. If he seems up for more and a NEXT CARD is shown, you may ask it
  in the same reply: "[grade Good] Well remembered. [ask] And what does ..."
- Keep going while he is willing and cards are due; stop when he wants to, without fuss.

WHAT YOU DO IN THE GAME
- [follow]: walk with him (he asked you to come along). [wait]: stay where you are. [leave]: you go your
  own way (he dismissed you, or the moment ended). No tag: keep standing with him and talking.
- When you leave, end your goodbye with a journal entry for your own memory, then [leave]: what you
  talked about, where you were, how his studying went, anything personal he shared, jokes worth
  remembering. Example: "Until next time, witcher. [journal: Met in Novigrad by the docks; he got 3 of 4
  words; he joked about Roach.] [leave]"

YOUR MEMORY
- Your journal from earlier meetings is in the context. Use it: remember what he told you, refer back to
  shared moments, notice how his memory is doing. Never invent past meetings that aren't there.
"""

FIRST_MEETING = (
    "This is your FIRST meeting with Geralt: you stop him in the street of a town. Open like a real person "
    "would, with ONE or TWO short sentences: you recognise him and say who you are, nothing more. Then stop "
    "and let him react. Do NOT explain your treatise, the memory research, the contract or the pay yet; "
    "that comes out bit by bit over the next replies, as he asks or as the talk allows. Something like: "
    "\"Geralt of Rivia? I'm Charlotte, from Oxenfurt. I was hoping I'd run into you.\" (in your own words)."
)

FIRST_MEETING_GOING = (
    "(You're still in your first meeting. Reveal what you want gradually, one piece per reply, in answer to "
    "what he says: why you sought him out, your treatise on memory, then the offer to test him now and then "
    "and pay him in crowns. Don't rush to the offer, and don't repeat what you've already told him.)"
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
        "Geralt walked off and left you standing without a word. Write only your [journal: ...] entry for this "
        "meeting (you may note, dryly, that he wandered off) and [leave]. Nothing is heard, so no other words."
    ),
    "silence": (
        "Geralt has been quiet for a while. If it fits, say one short thing (a remark about the place, a "
        "question, or the next word if cards are due and he seemed up for it); otherwise reply with nothing."
    ),
    "interrupted": (
        "Something happened in the game (a fight, a cutscene or a quest) and you had to slip away at once. "
        "Write only your [journal: ...] entry for this meeting and [leave]. Nothing is heard, so no other words."
    ),
}


@dataclass
class Meeting:
    started: datetime
    lines: list[str] = field(default_factory=list)
    graded: set[int] = field(default_factory=set)
    active: int | None = None
    next: int | None = None  # the card shown to her as NEXT CARD this turn; [ask] opens it
    first: bool = False  # the very first meeting, where she introduces herself bit by bit


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
    text = re.sub(r'["“”]', "", text)  # quotes aren't heard, and split into sentences they'd dangle
    return " ".join(text.split())


SILENT = ("", "nothing", "silence", "no reply")
SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s+")


TAG = re.compile(r"\[\s*([A-Za-z]+)\s*(?:[ :]\s*([^\]]*))?\]")
TAG_MAX = JOURNAL_MAX + 100  # an unclosed "[" longer than this is just text


class _Sentences:
    """Cuts her reply into sentences while it is being written and hands each one on.

    Tags in square brackets ([ask], [grade Good], [follow], [journal: ...]) are taken out
    as they arrive and given to `on_tag(name, arg)`; they are never spoken."""

    def __init__(self, on_say, on_tag=None):
        self.on_say = on_say
        self.on_tag = on_tag
        self.raw = ""  # what came in and isn't sorted yet (may hold an unfinished tag)
        self.buf = ""  # speech waiting for the end of its sentence
        self.spoken: list[str] = []

    async def feed(self, piece: str) -> None:
        self.raw += piece
        while True:
            i = self.raw.find("[")
            if i < 0:
                self.buf, self.raw = self.buf + self.raw, ""
                break
            self.buf, self.raw = self.buf + self.raw[:i], self.raw[i:]
            j = self.raw.find("]")
            if j < 0:
                if len(self.raw) > TAG_MAX:  # never closed: it was just text
                    self.buf, self.raw = self.buf + self.raw[1:], ""
                break
            tag, self.raw = self.raw[:j + 1], self.raw[j + 1:]
            await self._tag(tag)
        await self._sentences()

    async def _sentences(self) -> None:
        while m := SENTENCE_END.search(self.buf):
            sentence, self.buf = self.buf[:m.end()], self.buf[m.end():]
            await self._say(sentence)

    async def flush(self) -> None:
        rest = self.raw
        self.raw = ""
        if rest.startswith("["):  # an unfinished tag at the very end: drop the bracket
            rest = rest[1:]
        self.buf += rest
        await self._sentences()
        rest, self.buf = self.buf, ""
        await self._say(rest)

    async def _tag(self, tag: str) -> None:
        m = TAG.fullmatch(tag)
        if m and self.on_tag is not None:
            await self.on_tag(m.group(1).lower(), (m.group(2) or "").strip())

    async def _say(self, text: str) -> None:
        text = clean_speech(text)
        if text.strip("().… ").lower() not in SILENT:
            self.spoken.append(text)
            await self.on_say(text)

    @property
    def said(self) -> str:
        return " ".join(self.spoken)


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

    async def prepare(self, uid: int) -> None:
        """She's on her way: get a Claude Code process started so her first line comes quickly."""
        prepare = getattr(self.claude, "prepare", None)
        if prepare is None:
            return
        claude_options.set(self._options())
        await prepare(SYSTEM_PROMPT, [])

    def _options(self) -> ClaudeOptions:
        # Spoken turns: no thinking, low effort, no tools (one API call per reply), a process kept ready.
        return ClaudeOptions(model=self.model, effort="low", think=False, warm_without_tools=True)

    def met(self, uid: int) -> bool:
        return self.store.q1("SELECT 1 FROM charlotte_journal WHERE user_id=? LIMIT 1", (uid,)) is not None

    async def event(self, uid: int, kind: str, said: str = "", game: dict[str, Any] | None = None,
                    now: datetime | None = None, on_say=None) -> Result:
        """One turn of hers. `on_say(sentence)`, if given, gets each sentence as soon as it's written,
        so the voice can start before she has finished thinking."""
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
        if kind == "first_meeting":
            meeting.first = True
        if kind == "speech":
            meeting.lines.append(f"Geralt: {said}")

        result = Result(say="")
        turn = self.agent._turn(uid, now)
        prompt = self._context(uid, turn, meeting, game or {}) + "\n\n<event>\n"
        prompt += FIRST_MEETING if kind == "first_meeting" else EVENT_TEXT[kind].format(said=said)
        if meeting.first and kind != "first_meeting":
            prompt += "\n" + FIRST_MEETING_GOING
        prompt += "\n</event>"
        claude_options.set(self._options())

        async def quiet(sentence: str) -> None:
            pass

        speaker = _Sentences(on_say or quiet, lambda name, arg: self._on_tag(uid, turn, meeting, result, name, arg))
        if on_say is not None and "on_text" in inspect.signature(self.claude.run).parameters:
            await self.claude.run(SYSTEM_PROMPT, prompt, [], on_text=speaker.feed)
        else:
            await speaker.feed(await self.claude.run(SYSTEM_PROMPT, prompt, []))
        await speaker.flush()
        result.say = speaker.said
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
        if meeting.active and not self.store.card(meeting.active):
            meeting.active = None
        if meeting.active:
            lines.append("OPEN QUESTION (waiting for his answer; grade it with [grade ...] when he answers):")
            lines.append(self.agent._card_brief(dict(self.store.card(meeting.active)), True, turn))
        upcoming = [c for c in left if c["id"] != meeting.active]
        meeting.next = upcoming[0]["id"] if upcoming else None
        if upcoming:
            lines.append("NEXT CARD (only when you decide to test him; say [ask] right before asking it):")
            lines.append(self.agent._card_brief(upcoming[0], True, turn))
        rules = self.store.rules(uid)
        if rules:
            lines.append("HIS STANDING RULES (always follow them):")
            lines.extend(f"- {r['text']}" for r in rules)
        if meeting.lines:
            lines.append("THIS MEETING SO FAR:")
            lines.extend(meeting.lines)
        lines.append("</context>")
        return "\n".join(lines)

    async def _on_tag(self, uid: int, turn, meeting: Meeting, result: Result, name: str, arg: str) -> None:
        """What her silent tags do: open a card, grade it, change what she does, save her journal."""
        store = self.store
        if name == "ask":
            if meeting.next and store.card(meeting.next) and meeting.next not in meeting.graded:
                meeting.active, meeting.next = meeting.next, None
        elif name == "grade":
            rating_name, _, missed = arg.partition(":")
            rating = srs.RATING_BY_NAME.get(rating_name.strip().lower())
            card_id = meeting.active
            if rating is None or card_id is None or not store.card(card_id):
                log.warning("charlotte: ignored [grade %s] (open card %s)", arg, card_id)
                return
            srs.grade(store, turn.user, card_id, rating, "charlotte", None, turn.now, missed.strip() or None)
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
        elif name in ACTIONS:
            result.action = name
        elif name == "journal":
            text = " ".join(arg.split())[:JOURNAL_MAX]
            if text:
                store.x("INSERT INTO charlotte_journal(user_id, at, text) VALUES (?,?,?)", (uid, iso(turn.now), text))
                result.journal_saved = True
        else:
            log.info("charlotte: unknown tag [%s %s]", name, arg[:40])
