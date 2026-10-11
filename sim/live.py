"""Live end-to-end runs: the real bot code AND the real model (Claude through the
Claude Agent SDK, the same path production uses), driven by a simulated user.

Usage: python live.py <scenario.json> [out_dir]

Scenario format:
{
  "setup": "imported_behind" | "on_track" | "small_fresh",
  "persona": "extra notes about Nick for this scenario (optional)",
  "seed": 1,
  "steps": [
    {"wait": "2026-10-10 09:30"}           real timer runs every 5 minutes until then (local Berlin time)
    {"wait_min": 30}
    {"say": "literal text"}                 Nick writes exactly this
    {"user": "what Nick wants to get across"}  the simulated user writes it in his own words
    {"answer": "know|half|wrong|dunno"}    Nick answers the open card that way (simulated wording)
    {"tap_next": true}
    {"study": {"until": "2026-10-10 13:00", "know": 0.8, "delay": [2, 15], "skip": 0.0}}
        auto-play: whenever a card is open, answer it after a random delay (or leave it, with
        probability skip, until the next card or the end)
  ]
}

Output: <out>/transcript.txt (what Nick saw, plus tool calls marked with "·"),
<out>/prompts.log (every full prompt the model got).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = os.environ.get("MACAW_REPO", "/home/claude/macawsrs")

import driver  # noqa: E402  (sets the fake clock, builds the deck)
import logging  # noqa: E402
logging.getLogger("macaw").setLevel(logging.ERROR)

sys.path.insert(0, REPO)
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query  # noqa: E402

from macaw import srs  # noqa: E402
from macaw.agent import Agent  # noqa: E402
from macaw.bot import render  # noqa: E402
from macaw.bot.app import App  # noqa: E402
from macaw.config import Config  # noqa: E402
from macaw.llm import ToolSpec  # noqa: E402
from macaw.llm.claude import ClaudeCodeProvider  # noqa: E402
from macaw.templates import card_sides, deck_fields, headword, note_fields  # noqa: E402

CLOCK, TZ, UID, local = driver.CLOCK, driver.TZ, driver.UID, driver.local
BOT_MODEL = os.environ.get("BOT_MODEL", "sonnet")
USER_MODEL = os.environ.get("USER_MODEL", "sonnet")

OUT: list[str] = []
LOG = None
SEEN: list[str] = []  # what Nick saw, for the user simulator


def out(line: str) -> None:
    OUT.append(line)
    print(line, flush=True)


def stamp() -> str:
    return CLOCK[0].astimezone(TZ).strftime("%a %d %H:%M")


class LoggingLLM:
    def __init__(self):
        self.real = ClaudeCodeProvider(model=BOT_MODEL)
        self.calls = 0

    async def run(self, system, prompt, tools, must_use_tool=False):
        self.calls += 1
        LOG.write(f"\n\n######## CALL {self.calls} at {stamp()} ########\n{prompt}\n")

        def wrap(t: ToolSpec) -> ToolSpec:
            async def h(args):
                res = await t.handler(args)
                out(f"           · {t.name}({json.dumps(args, ensure_ascii=False)}) -> {res[:400]}")
                LOG.write(f"\n-- tool {t.name}({json.dumps(args)}) -> {res}\n")
                return res

            return ToolSpec(t.name, t.description, t.schema, h)

        for attempt in range(3):
            try:
                reply = await self.real.run(system, prompt, [wrap(t) for t in tools], must_use_tool)
                break
            except Exception as e:  # transient CLI failure: retry
                out(f"           · (model error, retrying: {e})")
                await asyncio.sleep(5)
        else:
            raise RuntimeError("model unavailable")
        LOG.write(f"\n-- reply: {reply}\n")
        return reply


class FakeBot(driver.FakeBot):
    async def send_message(self, chat_id, text, **kw):
        kb = kw.get("reply_markup")
        buttons = ""
        if kb:
            buttons = "  [" + " | ".join(b.text for row in kb.inline_keyboard for b in row) + "]"
        if text.startswith("<b>") or "Graded" in text or "<i>" in text:  # grade note / preview
            import re

            text = re.sub(r"<[^>]+>", "", text)
            out(f"{stamp()}     (note) {text.strip()}{buttons}")
            SEEN.append(f"[{stamp()}] (grade note) {text.strip()}")
        else:
            out(f"{stamp()}  BOT: {text}{buttons}")
            SEEN.append(f"[{stamp()}] Bot: {text}")

        class M:
            message_id = 1

        return M()


PERSONA = """You are role-playing Nick, a real person, chatting on his phone with a Telegram study bot that \
quizzes him on English vocabulary (advanced words) with spaced repetition. Nick is not technical. He writes \
short, casual messages, often lowercase, with an occasional small typo, no emoji. He imported his old Anki \
deck (about 600 words) that he hadn't touched for over a year. He gets impatient with walls of text and \
robotic or made-up-sounding replies, and calls out nonsense. Stay in character. Output ONLY Nick's next \
message, nothing else: no quotes, no stage directions, no explanations."""


async def user_says(instruction: str, persona_extra: str) -> str:
    convo = "\n".join(SEEN[-14:])
    prompt = (
        f"The chat so far (latest last):\n{convo}\n\nNow it's {stamp()}. Write Nick's next message. "
        f"What he wants to get across: {instruction}"
    )
    system = PERSONA + ("\n\n" + persona_extra if persona_extra else "")
    opts = ClaudeAgentOptions(system_prompt=system, tools=[], max_turns=1, model=USER_MODEL,
                              setting_sources=[], extra_args={"no-session-persistence": None})
    text = ""
    for attempt in range(3):
        try:
            async for m in query(prompt=prompt, options=opts):
                if isinstance(m, ResultMessage) and m.result:
                    text = m.result
                elif isinstance(m, AssistantMessage):
                    t = "".join(b.text for b in m.content if isinstance(b, TextBlock))
                    text = t or text
            break
        except Exception:
            await asyncio.sleep(5)
    return text.strip().strip('"')


def active_card(store):
    st = store.state(UID)
    cid = st["active_card_id"]
    if not cid or not store.card(cid):
        return None
    c = store.card(cid)
    note = store.note(c["note_id"])
    deck = store.deck(note["deck_id"])
    prompt, answer = card_sides(deck_fields(deck), note_fields(note["fields"]), c["ord"])
    return cid, headword(prompt)[0], answer


HOW = {
    "know": "He knows this word. Give its meaning in his own casual words (not copying the definition).",
    "half": "He half-knows it: give a vague or only partly right meaning, maybe with 'i think'.",
    "wrong": "He confuses it with something else and gives a wrong meaning, confidently or not.",
    "dunno": "He doesn't remember it at all; say so briefly.",
}


async def answer_card(store, how: str, persona_extra: str) -> str | None:
    a = active_card(store)
    if a is None:
        return None
    _, word, meaning = a
    if word.startswith("testword"):
        n = word[len("testword"):]
        return {"know": f"test meaning {n}", "half": f"test meaning {n} i think?", "wrong": "no clue, something about cars?",
                "dunno": "no idea"}[how]
    return await user_says(
        f"Answer the bot's latest question about the word '{word}' (its real meaning: {meaning}). {HOW[how]}",
        persona_extra,
    )


async def main(path: str, outdir: str) -> None:
    global LOG
    sc = json.load(open(path))
    od = Path(outdir)
    od.mkdir(parents=True, exist_ok=True)
    LOG = open(od / "prompts.log", "w")
    rng = random.Random(sc.get("seed", 1))
    store = driver.build(sc.get("setup", "imported_behind"))
    llm = LoggingLLM()
    agent = Agent(store, llm)
    cfg = Config("t", frozenset({UID}), None, "UTC", "fake", None, "INFO")
    app = App(cfg, store, agent, FakeBot())
    persona = sc.get("persona", "")
    out(f"(scenario {Path(path).stem}; setup {sc.get('setup')}; bot model {BOT_MODEL}; start {stamp()})")
    t0 = time.time()

    async def tick_until(end):
        while CLOCK[0] + timedelta(minutes=5) <= end:
            CLOCK[0] += timedelta(minutes=5)
            await app.tick(CLOCK[0])
        CLOCK[0] = max(CLOCK[0], end)

    async def say(text):
        out(f"{stamp()}  NICK: {text}")
        SEEN.append(f"[{stamp()}] Nick: {text}")
        async with app.lock(UID):
            await app.run_llm(UID, UID, lambda: agent.on_user_message(UID, text, CLOCK[0]), notify=True)

    for step in sc["steps"]:
        if "comment" in step:
            out(f"\n--- {step['comment']} ---")
        elif "wait" in step:
            await tick_until(local(step["wait"]))
        elif "wait_min" in step:
            await tick_until(CLOCK[0] + timedelta(minutes=step["wait_min"]))
        elif "say" in step:
            await say(step["say"])
        elif "user" in step:
            await say(await user_says(step["user"], persona))
        elif "answer" in step:
            text = await answer_card(store, step["answer"], persona)
            if text is None:
                out(f"{stamp()}  (no open card to answer)")
            else:
                await say(text)
        elif "tap_next" in step:
            out(f"{stamp()}  NICK taps [Next card]")
            SEEN.append(f"[{stamp()}] Nick: (taps Next card)")
            async with app.lock(UID):
                await app.run_llm(UID, UID, lambda: agent.ask_now(UID, CLOCK[0]), notify=True)
        elif "study" in step:
            s = step["study"]
            end = local(s["until"])
            lo, hi = s.get("delay", [2, 15])
            pending = None  # (card_id, answer_at)
            while CLOCK[0] < end:
                a = active_card(store)
                if a and (pending is None or pending[0] != a[0]):
                    if rng.random() < s.get("skip", 0.0):
                        pending = (a[0], None)
                    else:
                        pending = (a[0], CLOCK[0] + timedelta(minutes=rng.randint(lo, hi)))
                if a and pending and pending[1] and CLOCK[0] >= pending[1]:
                    r = rng.random()
                    know = s.get("know", 0.8)
                    how = "know" if r < know else "half" if r < know + (1 - know) / 2 else rng.choice(["wrong", "dunno"])
                    await say(await answer_card(store, how, persona))
                    pending = None
                    continue
                await tick_until(min(end, CLOCK[0] + timedelta(minutes=5)))
    out(f"\n(END; {llm.calls} bot model calls; {time.time() - t0:.0f}s)")
    (od / "transcript.txt").write_text("\n".join(OUT) + "\n")
    LOG.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else str(HERE / "runs" / Path(sys.argv[1]).stem)))
