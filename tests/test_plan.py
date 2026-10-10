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
    # Once there's a pace, far behind means more than two days of the user's usual work.
    assert pacing.backlog_status(15, pace=6) == "far_behind"
    assert pacing.backlog_status(43, pace=32) == "behind"


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


def test_misses_in_learning_steps_dont_count_as_forgetting(store, user):
    make_deck(store, reviews_today=1, new=3)
    for c in srs.due_queue(store, user, NOW)[1:]:  # three brand-new words, all missed at first
        srs.grade(store, user, c["id"], 1, "claude", None, NOW)
        srs.grade(store, user, c["id"], 1, "claude", None, NOW + timedelta(minutes=2))
    review = srs.due_queue(store, user, NOW)[0]
    srs.grade(store, user, review["id"], 3, "claude", None, NOW)
    b = srs.backlog(store, user, NOW + timedelta(minutes=5))
    assert (b["again_rate"], b["studied_reviews"]) == (0.0, 1)
    line = "\n".join(plan.report(store, user, NOW + timedelta(minutes=5), plan.build(store, user, NOW)))
    assert "Forgotten lately: 0% of 1 reviews (not counting learning steps)." in line
    assert "more than a month overdue" not in line


def test_long_overdue_cards_are_expected_to_be_forgotten(store, user):
    """A deck not reviewed for a year: forgetting much more than the usual 10% is what the scheduler expects."""
    make_deck(store, reviews_overdue=4)
    for c in store.user_cards(UID):
        store.set_card_schedule(c["id"], {
            "state": srs.REVIEW, "step": None, "stability": 30.0, "difficulty": 5.0,
            "due": iso(NOW - timedelta(days=400)), "last_review": iso(NOW - timedelta(days=430)),
        })
    for i, c in enumerate(store.user_cards(UID)):
        srs.grade(store, user, c["id"], 1 if i == 0 else 3, "claude", None, NOW)
    b = srs.backlog(store, user, NOW + timedelta(minutes=5))
    assert b["long_overdue"] == 4 and b["again_rate"] == 0.25
    assert b["expected_again"] > 0.3  # 430 days on a 30-day memory: far more than the usual 10%
    line = "\n".join(plan.report(store, user, NOW + timedelta(minutes=5), plan.build(store, user, NOW)))
    assert "4 of those reviews were cards more than a month overdue" in line
    assert "No new cards are waiting in the decks today" in line


def test_report_says_when_imported_cards_outrun_the_pace(store, user):
    make_deck(store, reviews_today=21)
    cards = store.user_cards(UID)
    for c in cards[:2]:  # yesterday: two answers, so the pace is 2 a day
        srs.grade(store, user, c["id"], 3, "claude", None, NOW - timedelta(days=1))
    for c in cards:  # 21 cards due tomorrow: 3 a day over the week, more than 2
        sched = srs.schedule_of(c)
        sched["due"] = iso(NOW + timedelta(days=1))
        store.set_card_schedule(c["id"], sched)
    line = "\n".join(plan.report(store, user, NOW, plan.build(store, user, NOW)))
    assert "more than the 2 a day done lately" in line
    assert "19 studied cards came in with an Anki import and haven't been reviewed here yet" in line


@pytest.mark.asyncio
async def test_done_for_today_means_nothing_more_today(agent, llm, store, user):
    make_deck(store, reviews_today=5)
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))
    await agent.ask_next(UID, NOW)

    async def done(prompt, t):
        out = await t["pause_reviews"]({"rest_of_day": True})
        assert out.startswith("Paused for the rest of today")
        return "Sure, tomorrow then."

    llm.script.append(done)
    await agent.on_user_message(UID, "im done for today", NOW + timedelta(minutes=1))
    st = store.state(UID)
    assert st["active_card_id"] is None
    assert st["next_ask_at"] == iso(srs.next_day_start(user, NOW))


def test_rounds_grow_at_most_twice_the_plan_unless_the_user_chose_them(store, user):
    make_deck(store, reviews_overdue=60, reviews_today=40)
    late = NOW + timedelta(hours=9)  # little of the day left for a lot of cards
    plan.set_today(store, user, NOW, 0, 3, None, "bot")
    p = plan.build(store, user, late)
    assert (p.planned_round, p.round_size) == (3, 6)
    assert "6 cards a round now (planned 3" in "\n".join(plan.report(store, user, late, p))
    plan.set_today(store, user, NOW, 0, 3, "he wants small rounds", "bot", fixed_round=True)
    assert plan.build(store, user, late).round_size == 3


def test_any_due_card_done_counts_toward_todays_target(store, user):
    make_deck(store, reviews_overdue=60, reviews_today=5)
    p = plan.build(store, user, NOW)
    assert p.goal_left == 25
    line = "\n".join(plan.report(store, user, NOW, p))
    assert "Still to do today: 25 (today's target is all of today's scheduled cards plus about 20" in line
    # Doing more left-over cards than today's share still brings today's count down.
    for c in [c for c in srs.due_queue(store, user, NOW) if srs._left_over(c, srs.day_start(user, NOW), NOW)][:24]:
        srs.grade(store, user, c["id"], 3, "claude", None, NOW)
    assert plan.build(store, user, NOW + timedelta(minutes=1)).goal_left == 1


@pytest.mark.asyncio
async def test_more_new_cards_on_request_when_todays_are_done(agent, llm, store, user):
    make_deck(store, new=8, new_per_day=20)
    plan.set_today(store, user, NOW, 0, 3, None, "bot")

    async def more(prompt, t):
        out = await t["next_card"]({"count": 5, "user_asked_now": True})
        assert "8 new cards are still waiting" in out
        await t["set_today_plan"]({"new_cards": 5, "cards_per_round": 3})
        out = await t["next_card"]({"count": 5, "user_asked_now": True})
        assert out.startswith("Ask this card now")
        return "Here's one: what does w0 mean?"

    llm.script.append(more)
    await agent.on_user_message(UID, "give me 5 more", NOW)


@pytest.mark.asyncio
async def test_writing_first_in_the_day_gets_the_check_in(agent, llm, store):
    make_deck(store, reviews_overdue=12)
    llm.script.append(lambda p, t: _ret("Morning!"))
    await agent.on_user_message(UID, "morning", NOW)
    assert "today's plan isn't set yet" in llm.prompts[-1]


def test_a_round_size_the_user_chose_holds_on_later_days(store, user):
    make_deck(store, reviews_overdue=60, reviews_today=40)
    plan.set_today(store, user, NOW, 0, 2, "he asked for 2", "bot", fixed_round=True)
    tomorrow = NOW + timedelta(days=1)
    user = dict(store.get_user(UID))
    p = plan.build(store, user, tomorrow + timedelta(hours=9))
    assert (p.round_size, p.suggested_round, p.fixed_round) == (2, 2, True)


def test_a_lighter_day_stops_at_the_cards_asked_for(store, user):
    make_deck(store, reviews_overdue=30, reviews_today=20)
    plan.set_today(store, user, NOW, 0, 3, "tired", "bot", max_cards=4)
    for c in srs.due_queue(store, user, NOW)[:4]:
        srs.grade(store, user, c["id"], 3, "claude", None, NOW)
    p = plan.build(store, user, NOW + timedelta(minutes=1))
    assert p.goal_left == 0
    assert "at most 4 cards today in all" in "\n".join(plan.report(store, user, NOW, p))


def test_quiet_hours_say_what_the_morning_will_look_like(store, user):
    make_deck(store, reviews_overdue=10, reviews_today=5)
    night = NOW.replace(hour=23)  # 01:00 in Berlin
    line = "\n".join(plan.report(store, user, night, plan.build(store, user, night)))
    assert "the next study day starts at 08:00" in line and "about 15 left over" in line


@pytest.mark.asyncio
async def test_stopping_for_the_day_is_not_being_ignored(agent, llm, store, user):
    make_deck(store, reviews_today=5)
    llm.script.append(lambda p, t: _ret("What does w0 mean?"))
    await agent.ask_next(UID, NOW)

    async def done(prompt, t):
        await t["pause_reviews"]({"rest_of_day": True})
        return "Tomorrow then."

    llm.script.append(done)
    await agent.on_user_message(UID, "done for today", NOW + timedelta(minutes=1))
    assert store.state(UID)["ignored_since"] is None


@pytest.mark.asyncio
async def test_next_card_at_a_clock_time(agent, llm, store, user):
    make_deck(store, reviews_today=5)

    async def later(prompt, t):
        out = await t["set_next_card_time"]({"at": "15:00"})
        assert "at about 15:00" in out
        return "Sure."

    llm.script.append(later)
    await agent.on_user_message(UID, "ask me at 15:00", NOW)
    assert store.state(UID)["next_ask_at"] == iso(at("2026-10-07 13:00"))


@pytest.mark.asyncio
async def test_a_card_waiting_for_its_delete_button_is_not_asked(agent, llm, store, user):
    make_deck(store, reviews_today=2)
    first = srs.due_queue(store, user, NOW)[0]

    async def delete(prompt, t):
        await t["delete_card"]({"note_id": first["note_id"]})
        return "Tap to confirm."

    llm.script.append(delete)
    await agent.on_user_message(UID, "delete w0", NOW)
    assert first["id"] not in [c["id"] for c in srs.due_queue(store, user, NOW + timedelta(minutes=1))]
