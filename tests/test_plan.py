from __future__ import annotations

from datetime import timedelta

import pytest

from macaw import pacing, plan, srs
from macaw.agent import Agent, Text
from macaw.db import iso

from .conftest import UID, FakeLLM, at

NOW = at("2026-10-07 10:00")  # 12:00 in Berlin; the study day started at 08:00


async def _ret(text):
    return text


def make_deck(store, reviews_overdue=0, reviews_today=0, new=0, new_per_day=20):
    """A deck with review cards left over from earlier days, review cards due today, and new cards."""
    deck_id = store.create_deck(UID, "English", "vocabulary", ["word", "meaning", "example", "notes"])
    store.update_deck(deck_id, new_per_day=new_per_day)
    i = 0
    for kind, n in (("overdue", reviews_overdue), ("today", reviews_today), ("new", new)):
        for _ in range(n):
            store.add_note(deck_id, {"word": f"w{i}", "meaning": f"m{i}"}, f"w{i}", reverse=False)
            card = max(store.user_cards(UID), key=lambda c: c["id"])
            if kind != "new":
                due = NOW - timedelta(days=3) if kind == "overdue" else NOW - timedelta(minutes=30)
                store.set_card_schedule(card["id"], {
                    "state": srs.REVIEW, "step": None, "stability": 5.0, "difficulty": 5.0,
                    "due": iso(due), "last_review": iso(due - timedelta(days=5)),
                })
            i += 1
    return deck_id


# ---------- pure rules ----------


def test_backlog_status_thresholds():
    assert pacing.backlog_status(0) == "on_track"
    assert pacing.backlog_status(3) == "slipping"
    assert pacing.backlog_status(12) == "behind"
    assert pacing.backlog_status(30) == "far_behind"
    # Two days of the user's usual work left over is far behind too.
    assert pacing.backlog_status(15, pace=6) == "far_behind"


def test_new_cards_slow_down_with_a_backlog():
    assert pacing.new_card_cap(20, "on_track") == 20
    assert pacing.new_card_cap(20, "slipping") == 10
    assert pacing.new_card_cap(20, "behind") == 5
    assert pacing.new_card_cap(20, "far_behind") == 0
    assert pacing.new_card_cap(20, "on_track", hard=True) == 10


def test_backlog_is_cleared_over_a_few_days():
    assert pacing.catch_up_days(0) == 0
    assert pacing.catch_up_days(20) == 1
    assert pacing.catch_up_days(44) == 2
    assert pacing.backlog_quota(44) == 22
    assert pacing.catch_up_days(1000) == pacing.MAX_CATCH_UP_DAYS


def test_rounds_grow_when_the_day_wont_fit():
    end = NOW + timedelta(hours=12)  # 18 rounds of 40 minutes left
    assert pacing.round_size(NOW, end, 10, 1) == 1
    assert pacing.round_size(NOW, end, 44, 1) == 3
    assert pacing.round_size(NOW, end, 44, 5) == 5  # never below the user's own setting
    assert pacing.round_size(end - timedelta(minutes=30), end, 500, 1) == pacing.MAX_ROUND


# ---------- what is due ----------


def test_no_new_cards_while_far_behind(store, user):
    make_deck(store, reviews_overdue=35, new=10)
    queue = srs.due_queue(store, user, NOW)
    assert len(queue) == 35
    assert all(c["state"] == srs.REVIEW for c in queue)


def test_a_few_new_cards_while_behind(store, user):
    make_deck(store, reviews_overdue=12, new=10)
    queue = srs.due_queue(store, user, NOW)
    assert sum(1 for c in queue if c["state"] == srs.NEW) == 5


def test_all_new_cards_when_on_track(store, user):
    make_deck(store, reviews_today=5, new=10)
    queue = srs.due_queue(store, user, NOW)
    assert sum(1 for c in queue if c["state"] == srs.NEW) == 10


def test_plan_counts_todays_share_of_the_backlog(store, user):
    make_deck(store, reviews_overdue=44, reviews_today=4, new=10)
    p = plan.build(store, user, NOW)
    assert p.status == "far_behind"
    assert (p.overdue, p.quota, p.catch_up_days) == (44, 22, 2)
    assert p.new_cap == 0 and p.new_left == 0
    assert p.goal_left == 4 + 22
    assert p.round_size == 2  # 26 cards over the ~18 rounds left before midnight

    # Doing left-over cards counts toward today's share.
    first = srs.due_queue(store, user, NOW)[0]
    srs.grade(store, user, first["id"], 3, "claude", None, NOW)
    p = plan.build(store, user, NOW + timedelta(minutes=1))
    assert (p.overdue, p.overdue_at_start, p.goal_left) == (43, 44, 25)


def test_report_shows_the_record_and_todays_cards(store, user):
    make_deck(store, reviews_overdue=12, reviews_today=4, new=10)
    yesterday = NOW - timedelta(days=1)
    srs.snapshot_day(store, user, yesterday)
    first = srs.due_queue(store, user, yesterday)[0]
    srs.grade(store, user, first["id"], 1, "claude", None, yesterday)
    srs.snapshot_day(store, user, NOW)
    p = plan.build(store, user, NOW)
    lines = "\n".join(plan.report(store, user, NOW, p))
    assert "Last 7 study days:" in lines
    assert "Wed 30 Sep" not in lines  # before any record
    assert (
        "Tue 06 Oct: 12 reviews due (12 of them left over from before); 1 answers on 1 cards, "
        "0 new started, 1 of 1 reviews forgotten; 12 left over at the end"
    ) in lines
    assert "Today's due cards: 12 left over from earlier days, 4 reviews scheduled for today" in lines
    assert "TODAY'S PLAN: not set yet. The code suggests 0 new cards (the decks allow 10" in lines


def test_plan_set_for_today_decides_the_new_cards(store, user):
    make_deck(store, reviews_today=3, new=10)
    p = plan.set_today(store, user, NOW, new_cards=2, round_size=3, reason="easy start", set_by="bot")
    assert sum(1 for c in srs.due_queue(store, user, NOW) if c["state"] == srs.NEW) == 2
    assert (p.new_cap, p.round_size, p.target, p.set_by) == (2, 3, 5, "bot")
    # Limits hold whatever the bot asks for.
    p = plan.set_today(store, user, NOW, new_cards=500, round_size=99, reason=None, set_by="bot")
    assert (p.new_cap, p.round_size) == (10, pacing.MAX_ROUND)


# ---------- the bot acting on it ----------


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def agent(store, llm):
    return Agent(store, llm)


@pytest.mark.asyncio
async def test_first_round_of_the_day_is_the_teachers_check_in(agent, llm, store, user):
    make_deck(store, reviews_overdue=12, new=10)
    events = []

    async def check_in(prompt, t):
        events.append(prompt.split("<event>")[1])
        out = await t["set_today_plan"]({"new_cards": 0, "cards_per_round": 3, "reason": "clear the pile first"})
        assert "Today's plan saved: 0 new cards, 3 cards a round" in out
        return "Yesterday left 12 cards over, so today it's those 12 and no new words, 3 at a time. What does w0 mean?"

    llm.script.append(check_in)
    actions = await agent.ask_next(UID, NOW)
    assert "daily check-in" in events[-1]
    assert "PROGRESS REPORT" in llm.prompts[-1]
    assert [a.text for a in actions if isinstance(a, Text)][0].startswith("Yesterday left 12")
    assert store.day_plan(UID, "2026-10-07")["set_by"] == "bot"
    assert not any(c["state"] == srs.NEW for c in srs.due_queue(store, user, NOW))

    # The rest of the day: no more check-ins.
    store.update_state(UID, active_card_id=None, asked_at=None)
    llm.script.append(lambda p, t: _ret("What does w1 mean?"))
    await agent.ask_next(UID, NOW + timedelta(minutes=30))
    assert "daily check-in" not in llm.prompts[-1].split("<event>")[1]


@pytest.mark.asyncio
async def test_a_skipped_check_in_is_written_again_and_the_plan_saved_anyway(agent, llm, store):
    make_deck(store, reviews_overdue=12, new=10)
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))

    async def again(prompt, t):
        assert "skipped the check-in" in prompt.split("<event>")[1]
        return "You've got 12 left over from before, so no new words today. What does w0 mean?"

    llm.script.append(again)
    actions = await agent.ask_next(UID, NOW)
    assert [a.text for a in actions if isinstance(a, Text)] == [
        "You've got 12 left over from before, so no new words today. What does w0 mean?"
    ]
    assert store.day_plan(UID, "2026-10-07")["set_by"] == "code"


@pytest.mark.asyncio
async def test_falling_behind_the_plan_is_said_with_the_next_card(agent, llm, store, user):
    make_deck(store, reviews_today=30)
    plan.set_today(store, user, NOW - timedelta(hours=4), new_cards=0, round_size=2, reason=None, set_by="bot")
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))
    await agent.ask_next(UID, NOW + timedelta(hours=4))  # 18:00 in Berlin, nothing done since 08:00
    event = llm.prompts[-1].split("<event>")[1]
    assert "cards behind an even pace" in event
    assert store.state(UID)["nudge_at"]


@pytest.mark.asyncio
async def test_rounds_get_bigger_while_behind(agent, llm, store):
    make_deck(store, reviews_overdue=44)
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))
    await agent.ask_next(UID, NOW)
    first = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": first, "rating": "Good"})
        assert "Continue the session" in out
        return "Right. And w1?"

    llm.script.append(grade)
    await agent.on_user_message(UID, "m0", NOW + timedelta(minutes=1))
    assert store.state(UID)["active_card_id"] not in (None, first)


@pytest.mark.asyncio
async def test_pause_while_behind_gets_pushback_once_then_is_respected(agent, llm, store):
    make_deck(store, reviews_overdue=20)
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))
    await agent.ask_next(UID, NOW)

    async def stop(prompt, t):
        out = await t["pause_reviews"]({})
        assert out.startswith("NOT paused")
        return "Stopping now means..."

    llm.script.append(stop)
    await agent.on_user_message(UID, "stop", NOW + timedelta(minutes=1))
    assert store.state(UID)["active_card_id"]  # the question is still open

    async def insist(prompt, t):
        out = await t["pause_reviews"]({"insist": True})
        assert "withdrawn" in out
        return "Okay."

    llm.script.append(insist)
    await agent.on_user_message(UID, "no, stop", NOW + timedelta(minutes=2))
    assert store.state(UID)["active_card_id"] is None

    # Later the same day a stop is simply respected.
    store.update_state(UID, active_card_id=srs.due_queue(store, dict(store.get_user(UID)), NOW)[0]["id"],
                       asked_at=iso(NOW + timedelta(hours=2)))

    async def stop_again(prompt, t):
        out = await t["pause_reviews"]({})
        assert "withdrawn" in out
        return "Sure."

    llm.script.append(stop_again)
    await agent.on_user_message(UID, "stop", NOW + timedelta(hours=2, minutes=1))


@pytest.mark.asyncio
async def test_no_pushback_when_on_track(agent, llm, store):
    make_deck(store, reviews_today=3)
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))
    await agent.ask_next(UID, NOW)

    async def stop(prompt, t):
        out = await t["pause_reviews"]({})
        assert "withdrawn" in out
        return "Sure."

    llm.script.append(stop)
    await agent.on_user_message(UID, "stop", NOW + timedelta(minutes=1))
