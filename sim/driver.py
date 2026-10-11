"""Replay a scripted study day through the real bot code (ticker, agent, tools, plan).

Usage: python driver.py <setup> <script.json>

setup: imported_behind | on_track
script.json: {"steps": [...], "llm": [...]}
  steps, run in order:
    {"wait": "2026-10-10 09:30"}   local Berlin time; the real ticker runs every 5 minutes until then
    {"wait_min": 3}                  same, for a number of minutes
    {"say": "text"}                  Nick writes a message now
    {"tap_next": true}               Nick taps the Next card button
    {"tap_grade": 3}                 Nick taps a grade button (1-4) on the latest grade note
  llm: the bot's turns, consumed in order whenever the code calls the model:
    {"tools": [{"name": "grade_card", "args": {...}}, ...], "reply": "text"}
    Omit "reply" to run the tools, see their results, and stop.

When the code calls the model and no llm entry is left, the driver prints the
prompt (context + event) and the tools, then stops: write the next llm entry
and run again. The run is deterministic, so it replays to the same point.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import os  # noqa: E402
sys.path.insert(0, os.environ.get("MACAW_REPO", "/home/claude/macawsrs"))

import macaw.bot.app as appmod  # noqa: E402
import macaw.db as dbmod  # noqa: E402
from macaw import srs  # noqa: E402
from macaw.agent import Agent  # noqa: E402
from macaw.bot import render  # noqa: E402
from macaw.bot.app import App  # noqa: E402
from macaw.config import Config  # noqa: E402
from macaw.db import Store, iso  # noqa: E402

from words import WORDS  # noqa: E402

UID = 42
TZ = ZoneInfo("Europe/Berlin")
CLOCK = [datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)]
dbmod.utcnow = lambda: CLOCK[0]
appmod._now = lambda: CLOCK[0]


def local(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).astimezone(timezone.utc)


class Stop(Exception):
    pass


class ScriptedLLM:
    def __init__(self, turns):
        self.turns = list(turns)
        self.n = 0

    async def run(self, system, prompt, tools, must_use_tool=False):
        self.n += 1
        if not self.turns:
            print(f"\n=========== MODEL CALL #{self.n} (needs an llm entry) ===========")
            print("(system prompt: macaw/prompts.py SYSTEM_PROMPT)")
            if must_use_tool:
                print("(must call at least one tool this turn)")
            print(prompt)
            print("\n----- tools -----")
            for t in tools:
                print(f"{t.name}: {t.description}\n  args: {json.dumps(t.schema.get('properties', {}))}")
            raise Stop()
        turn = self.turns.pop(0)
        by_name = {t.name: t for t in tools}
        for call in turn.get("tools", []):
            t = by_name.get(call["name"])
            out = await t.handler(call.get("args", {})) if t else f"(no tool named {call['name']})"
            print(f"    [tool {call['name']}({json.dumps(call.get('args', {}))}) -> {out}]")
        if "reply" not in turn:
            print("\n(stopped after the tools: add the reply to this llm entry)")
            raise Stop()
        return turn["reply"]


class FakeBot:
    async def send_message(self, chat_id, text, **kw):
        kb = kw.get("reply_markup")
        buttons = ""
        if kb:
            buttons = "  [" + " | ".join(b.text for row in kb.inline_keyboard for b in row) + "]"
        stamp = CLOCK[0].astimezone(TZ).strftime("%a %H:%M")
        print(f"{stamp}  BOT: {text}{buttons}")

        class M:
            message_id = 1

        return M()

    async def edit_message_text(self, text, **kw):
        print(f"           (grade note edited) {text}")

    async def send_chat_action(self, *a, **kw):
        pass

    async def delete_message(self, *a, **kw):
        pass


def build(setup: str) -> Store:
    random.seed(7)
    s = Store(":memory:")
    s.ensure_user(UID, UID, "Europe/Berlin")
    deck = s.create_deck(UID, "English", "vocabulary", ["word", "meaning", "example", "notes"])
    user = dict(s.get_user(UID))
    if setup == "on_track":
        T0 = local("2026-10-10 07:00")
        for i, (w, ex, m) in enumerate(WORDS[:80]):
            s.add_note(deck, {"word": f"{w}\n{ex}", "meaning": m}, w, reverse=False)
            c = max(s.user_cards(UID), key=lambda c: c["id"])
            stab = random.choice([4, 10, 25, 60])
            if i < 14:
                due = T0 + timedelta(hours=random.randint(2, 10))
            else:
                due = T0 + timedelta(days=random.randint(1, 30))
            s.set_card_schedule(c["id"], {"state": 2, "step": None, "stability": float(stab), "difficulty": 5.0,
                                          "due": iso(due), "last_review": iso(due - timedelta(days=stab))})
        s.update_deck(deck, new_per_day=10)
        for w, ex, m in WORDS[80:110]:
            s.add_note(deck, {"word": f"{w}\n{ex}", "meaning": m}, w, reverse=False)
        # three easy days before
        for d in (3, 2, 1):
            t = T0 - timedelta(days=d) + timedelta(hours=3)
            for c in random.sample(s.user_cards(UID)[:80], 12):
                sc = srs.schedule_of(c)
                if sc["state"] == 0:
                    continue
                srs.grade(s, user, c["id"], 3 if random.random() < 0.92 else 1, "claude", None, t)
                t += timedelta(minutes=20)
            CLOCK[0] = T0 - timedelta(days=d)
            srs.snapshot_day(s, user, CLOCK[0])
        # keep today's 14 due today after the regrades above
        for i, c in enumerate(sorted(s.user_cards(UID), key=lambda c: c["id"])[:14]):
            sc = srs.schedule_of(c)
            sc["due"] = iso(T0 + timedelta(hours=1 + i % 8))
            s.set_card_schedule(c["id"], sc)
        CLOCK[0] = T0
        return s

    # imported_behind: Nick's ~600-card Anki deck, untouched for over a year, no new words,
    # overdue cards spread at 40 a day on import (Wed 7 Oct), then three days of patchy study.
    T0 = local("2026-10-07 10:00")
    sched = srs.scheduler_for(user)
    for i in range(600):
        if i < len(WORDS):
            word, ex, m = WORDS[i]
        else:  # padding: distinct made-up items, so real words never repeat
            word, ex, m = f"testword{i}", f"An example with testword{i}.", f"test meaning {i}"
        s.add_note(deck, {"word": f"{word}\n{ex}", "meaning": m}, word, reverse=False)
        c = max(s.user_cards(UID), key=lambda c: c["id"])
        stab = random.choice([3, 8, 20, 45, 90, 200])
        last = T0 - timedelta(days=420 + random.randint(0, 60))
        s.set_card_schedule(c["id"], {"state": 2, "step": None, "stability": float(stab), "difficulty": 5.0,
                                      "due": iso(last + timedelta(days=stab)), "last_review": iso(last)})
    cards = sorted(s.user_cards(UID), key=lambda c: -srs.recall_at(sched, dict(c), T0))
    ds0 = srs.day_start(user, T0)
    for i, c in enumerate(cards):
        if i // 40:
            sc = srs.schedule_of(c)
            sc["due"] = iso(ds0 + timedelta(days=i // 40))
            s.set_card_schedule(c["id"], sc)
    for d, n in ((0, 45), (1, 30), (2, 20)):
        day = T0 + timedelta(days=d)
        CLOCK[0] = day
        srs.snapshot_day(s, user, day)
        t = day + timedelta(hours=1)
        for _ in range(n):
            q = srs.due_queue(s, user, t)
            if not q:
                break
            c = q[0]
            r = srs.recall_at(sched, dict(c), t) if c["state"] == 2 else 0.8
            srs.grade(s, user, c["id"], 3 if random.random() < r else 1, "claude", None, t)
            t += timedelta(minutes=random.randint(3, 40))
    CLOCK[0] = local("2026-10-10 07:30")
    return s


async def main(setup: str, script_path: str) -> None:
    script = json.load(open(script_path)) if script_path else {"steps": [], "llm": []}
    store = build(setup)
    llm = ScriptedLLM(script.get("llm", []))
    agent = Agent(store, llm)
    cfg = Config("t", frozenset({UID}), None, "UTC", "fake", None, "INFO")
    app = App(cfg, store, agent, FakeBot())
    print(f"(setup {setup}; now {CLOCK[0].astimezone(TZ):%a %d %b %H:%M})")

    async def tick_until(end: datetime) -> None:
        while CLOCK[0] + timedelta(minutes=5) <= end:
            CLOCK[0] += timedelta(minutes=5)
            await app.tick(CLOCK[0])
        CLOCK[0] = max(CLOCK[0], end)

    for step in script.get("steps", []):
        if "wait" in step:
            await tick_until(local(step["wait"]))
        elif "wait_min" in step:
            await tick_until(CLOCK[0] + timedelta(minutes=step["wait_min"]))
        elif "say" in step:
            print(f"{CLOCK[0].astimezone(TZ):%a %H:%M}  NICK: {step['say']}")
            async with app.lock(UID):
                await app.run_llm(UID, UID, lambda: agent.on_user_message(UID, step["say"], CLOCK[0]), notify=True)
        elif "tap_next" in step:
            print(f"{CLOCK[0].astimezone(TZ):%a %H:%M}  NICK taps [Next card]")
            async with app.lock(UID):
                await app.run_llm(UID, UID, lambda: agent.ask_now(UID, CLOCK[0]), notify=True)
        elif "tap_grade" in step:
            r = store.q("SELECT id FROM review_log ORDER BY id DESC LIMIT 1")[0]
            print(f"{CLOCK[0].astimezone(TZ):%a %H:%M}  NICK taps grade {srs.RATING_NAMES[step['tap_grade']]}")
            u = dict(store.get_user(UID))
            srs.regrade(store, u, r["id"], step["tap_grade"])
            text, _ = render.rating_note(store, r["id"])
            print(f"           (grade note now) {text}")
    print("\n(END of script)")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
    except Stop:
        pass
