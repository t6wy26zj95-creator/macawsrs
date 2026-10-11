from __future__ import annotations

import re

from datetime import timedelta

import pytest

from macaw import srs
from macaw.agent import REGRADE_WINDOW, Agent, ConfirmDelete, Preview, RatingNote, Text
from macaw.bot.app import App
from macaw.config import Config
from macaw.db import iso

from .conftest import UID, FakeLLM, at

pytestmark = pytest.mark.asyncio

NOW = at("2026-10-07 10:00")  # 12:00 in Berlin


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def agent(store, llm):
    return Agent(store, llm)


async def _make_deck_with_card(agent, llm, word="ubiquitous", meaning="present everywhere"):
    async def step(prompt, t):
        await t["create_deck"]({"name": "English", "deck_type": "vocabulary"})
        await t["propose_card"]({"deck": "English", "fields": {"word": word, "meaning": meaning}})
        return "Here's your card!"

    llm.script.append(step)
    actions = await agent.on_user_message(UID, f"add {word}", NOW)
    preview = next(a for a in actions if isinstance(a, Preview))
    assert agent.add_proposal(preview.proposal_id, UID).startswith("Added")
    return preview


async def test_add_card_via_preview(agent, llm, store):
    actions_preview = await _make_deck_with_card(agent, llm)
    assert store.count_notes(store.deck_by_name(UID, "English")["id"]) == 1
    assert store.proposal(actions_preview.proposal_id)["status"] == "added"


async def test_duplicate_is_reported_not_previewed(agent, llm, store):
    await _make_deck_with_card(agent, llm)

    async def step(prompt, t):
        await t["propose_card"]({"deck": "English", "fields": {"word": "Ubiquitous", "meaning": "x"}})
        return "You already have it."

    llm.script.append(step)
    actions = await agent.on_user_message(UID, "add ubiquitous again", NOW)
    assert not any(isinstance(a, Preview) for a in actions)
    assert llm.tool_results[-1].startswith("Duplicate")


async def test_ask_grade_and_pause(agent, llm, store):
    await _make_deck_with_card(agent, llm)

    async def ask(prompt, t):
        assert "ACTIVE CARD (asked" in prompt
        return "By the way, what does 'ubiquitous' mean?"

    llm.script.append(ask)
    actions = await agent.ask_next(UID, NOW)
    assert isinstance(actions[0], Text)
    card_id = store.state(UID)["active_card_id"]
    assert card_id

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": card_id, "rating": "Good", "reason": "spot on"})
        assert "Session done" in out or "last card" in out
        return "Exactly!"

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "it means everywhere", NOW + timedelta(minutes=1))
    assert isinstance(actions[0], Text)
    assert any(isinstance(a, RatingNote) for a in actions)
    st = store.state(UID)
    assert st["active_card_id"] is None
    assert st["next_ask_at"] is not None


async def test_revealing_the_answer_postpones_the_card(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean? Hint: present everywhere!"))
    await agent.ask_next(UID, NOW)
    st = store.state(UID)
    assert st["active_card_id"] is None  # postponed instead of graded
    card = store.user_cards(UID)[0]
    assert card["buried_until"] is not None
    assert card["state"] == 0


async def test_user_mentioning_due_answer_postpones_it(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("Nice sentence!"))
    await agent.on_user_message(UID, "Phones are present everywhere nowadays", NOW)
    assert store.user_cards(UID)[0]["buried_until"] is not None


async def test_more_cards_in_a_row(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")

    async def more(prompt, t):
        out = await t["next_card"]({"count": 2})
        assert "Ask this card now" in out
        return "Sure! What does ubiquitous mean?"

    llm.script.append(more)
    await agent.on_user_message(UID, "give me two cards", NOW)
    first = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": first, "rating": "Good", "reason": "ok"})
        assert "Continue the session" in out
        return "Right! Next: serendipity?"

    llm.script.append(grade)
    await agent.on_user_message(UID, "everywhere", NOW + timedelta(minutes=1))
    second = store.state(UID)["active_card_id"]
    assert second and second != first


async def test_answer_after_a_reminder_pauses_the_session(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    store.update_user(UID, cards_per_session=3)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]
    llm.script.append(lambda p, t: _ret("Still thinking about ubiquitous?"))
    await agent.remind(UID, NOW + timedelta(hours=1))

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        assert "Session done" in out
        return "Yes!"

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "everywhere", NOW + timedelta(hours=2))
    assert store.state(UID)["active_card_id"] is None
    assert next(a for a in actions if isinstance(a, RatingNote)).offer_next


async def test_answer_after_a_reminder_keeps_the_round_when_they_were_already_talking(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    store.update_user(UID, cards_per_session=3)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]
    llm.script.append(lambda p, t: _ret("Still thinking about ubiquitous?"))
    await agent.remind(UID, NOW + timedelta(hours=1))
    llm.script.append(lambda p, t: _ret("Sure, smaller rounds it is. So, ubiquitous?"))
    await agent.on_user_message(UID, "can we do smaller rounds", NOW + timedelta(hours=2))

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        assert "Session done" not in out
        return "Yes!"

    llm.script.append(grade)
    await agent.on_user_message(UID, "everywhere", NOW + timedelta(hours=2, minutes=1))


async def test_pause_withdraws_the_question_and_brings_it_back_later(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)

    async def stop(prompt, t):
        out = await t["pause_reviews"]({})
        assert "withdrawn" in out
        return "Sure."

    llm.script.append(stop)
    await agent.on_user_message(UID, "can you stop shooting questions", NOW + timedelta(minutes=1))
    st = store.state(UID)
    assert st["active_card_id"] is None
    assert st["next_ask_at"] > (NOW + timedelta(minutes=1)).isoformat()
    assert store.user_cards(UID)[0]["buried_until"] is None  # still due, just later


async def test_delete_needs_confirmation(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    note_id = store.notes_in_deck(store.deck_by_name(UID, "English")["id"])[0]["id"]

    async def step(prompt, t):
        await t["delete_card"]({"note_id": note_id})
        return "Confirm below."

    llm.script.append(step)
    actions = await agent.on_user_message(UID, "remove ubiquitous", NOW)
    assert any(isinstance(a, ConfirmDelete) for a in actions)
    assert store.note(note_id) is not None


async def test_settings_tool_validates_timezone(agent, llm, store):
    async def step(prompt, t):
        await t["update_settings"]({"timezone": "Europe/Moscow", "quiet_start": "0:00", "cards_per_session": 3})
        return "Done."

    llm.script.append(step)
    await agent.on_user_message(UID, "I'm in Moscow, 3 cards at a time", NOW)
    u = store.get_user(UID)
    assert (u["timezone"], u["quiet_start"], u["cards_per_session"]) == ("Europe/Moscow", "00:00", 3)


async def _ret(text):
    return text


# ---------- timer ----------


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

        class M:
            message_id = len(self.sent)

        return M()

    async def send_chat_action(self, *a, **kw):
        pass

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def _app(store, agent):
    cfg = Config("t", frozenset({UID}), None, "UTC", "fake", None, "INFO")
    return App(cfg, store, agent, FakeBot())


async def test_ticker_asks_then_reminds_then_respects_quiet_hours(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    llm.script.append(lambda p, t: _ret("Quick one: what's 'ubiquitous'?"))
    await app.tick(NOW + timedelta(minutes=5))
    assert app.bot.sent[-1].startswith("Quick one")
    asked = NOW + timedelta(minutes=5)

    llm.script.append(lambda p, t: _ret("Psst, still there?"))
    await app.tick(asked + timedelta(minutes=30))  # too early
    assert len(app.bot.sent) == 1
    await app.tick(asked + timedelta(minutes=61))
    assert app.bot.sent[-1] == "Psst, still there?"
    assert store.state(UID)["reminders_streak"] == 1

    # 23:30 UTC is 01:30 in Berlin: quiet hours, nothing is sent.
    await app.tick(at("2026-10-07 23:30"))
    assert len(app.bot.sent) == 2


async def test_reminders_used_on_an_earlier_card_dont_silence_the_next(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    # Earlier today another card used up all the reminders.
    store.update_state(UID, reminders_date=srs.day_start(store.get_user(UID), NOW).date().isoformat(),
                       reminders_today=4, reminders_streak=4, last_reminder_at=iso(NOW - timedelta(hours=1)))
    llm.script.append(lambda p, t: _ret("Here's one: what's 'ubiquitous'?"))
    await app.tick(NOW + timedelta(minutes=5))
    asked = NOW + timedelta(minutes=5)
    llm.script.append(lambda p, t: _ret("Still there?"))
    await app.tick(asked + timedelta(minutes=61))
    assert app.bot.sent[-1] == "Still there?"


async def test_an_empty_reminder_is_not_counted_as_sent(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    llm.script.append(lambda p, t: _ret("What's 'ubiquitous'?"))
    await app.tick(NOW + timedelta(minutes=5))
    asked = NOW + timedelta(minutes=5)
    llm.script.append(lambda p, t: _ret(""))
    await app.tick(asked + timedelta(minutes=61))
    assert len(app.bot.sent) == 1
    assert store.state(UID)["reminders_today"] == 0
    assert store.state(UID)["llm_backoff_until"]  # a short pause, then it tries again
    store.update_state(UID, llm_backoff_until=None)
    llm.script.append(lambda p, t: _ret("Psst, still there?"))
    await app.tick(asked + timedelta(minutes=90))
    assert app.bot.sent[-1] == "Psst, still there?"
    assert store.state(UID)["reminders_today"] == 1


async def test_session_ending_at_night_waits_for_morning(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    store.update_user(UID, cards_per_session=1)
    app = _app(store, agent)
    night = at("2026-10-07 22:15")  # 00:15 in Berlin, quiet hours
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, night)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        return "Right! It's getting late."

    llm.script.append(grade)
    await agent.on_user_message(UID, "everywhere", night + timedelta(minutes=4))
    llm.script.append(lambda p, t: _ret("Next: serendipity?"))
    await app.tick(night + timedelta(minutes=10))  # the user is still "around", but it's night
    assert app.bot.sent == []
    assert store.state(UID)["next_ask_at"] == iso(at("2026-10-08 06:00"))  # 08:00 in Berlin


@pytest.mark.filterwarnings("ignore")
def test_daily_backup_once(store, tmp_path):
    assert store.backup(tmp_path) is not None
    assert store.backup(tmp_path) is None
    assert len(list(tmp_path.glob("macaw-*.sqlite3"))) == 1


@pytest.mark.filterwarnings("ignore")
def test_strip_emoji():
    from macaw.agent import strip_emoji

    assert strip_emoji("Nice! 🎉 You got it 👍🏽") == "Nice! You got it"
    assert strip_emoji("Привет, 5 → 6") == "Привет, 5 → 6"


async def test_prompt_stays_small(agent, llm, store):
    for i in range(30):
        store.log_message(UID, "user", f"old message {i} " + "x" * 2000)
    llm.script.append(lambda p, t: _ret("ok"))
    await agent.on_user_message(UID, "newest " + "y" * 900, NOW)
    prompt = llm.prompts[-1]
    assert "old message 13 " not in prompt  # outside the history window
    assert "x" * 600 not in prompt  # older long messages are shortened
    assert "y" * 900 in prompt  # the newest message is kept whole, once
    assert prompt.count("y" * 900) == 1


async def test_context_lists_cards_coming_back_later(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Good", "reason": "ok"})
        return "Yes!"

    llm.script.append(grade)
    await agent.on_user_message(UID, "everywhere", NOW)
    llm.script.append(lambda p, t: _ret("It comes back at 12:10."))
    await agent.on_user_message(UID, "why 0 due?", NOW + timedelta(minutes=1))
    prompt = llm.prompts[-1]
    assert "Coming back later today (1" in prompt
    assert "1 due today" in prompt


async def test_new_version_of_a_card_replaces_the_old_preview(agent, llm, store):
    app = _app(store, agent)

    async def first(prompt, t):
        await t["create_deck"]({"name": "English", "deck_type": "vocabulary"})
        await t["propose_card"]({"deck": "English", "fields": {"word": "to pound down", "meaning": "to move heavily"}})
        return "Here it is."

    llm.script.append(first)
    await app.send_actions(UID, await agent.on_user_message(UID, "add pound down", NOW))
    old = store.pending_proposals(UID)[0]

    async def again(prompt, t):
        await t["propose_card"]({"deck": "English", "fields": {"word": "to pound down", "meaning": "to strike hard",
                                                              "example": "Rain pounded down on the roof."}})
        return "New example."

    llm.script.append(again)
    await app.send_actions(UID, await agent.on_user_message(UID, "new example please", NOW))
    assert store.proposal(old["id"])["status"] == "replaced"
    assert app.bot.deleted == [old["message_id"]]
    pending = store.pending_proposals(UID)
    assert len(pending) == 1 and pending[0]["message_id"] != old["message_id"]


async def test_revised_preview_is_resent_at_the_bottom(agent, llm, store):
    app = _app(store, agent)

    async def first(prompt, t):
        await t["create_deck"]({"name": "English", "deck_type": "vocabulary"})
        await t["propose_card"]({"deck": "English", "fields": {"word": "ubiquitous", "meaning": "everywhere"}})
        return "Here it is."

    llm.script.append(first)
    await app.send_actions(UID, await agent.on_user_message(UID, "add ubiquitous", NOW))
    p = store.pending_proposals(UID)[0]

    async def revise(prompt, t):
        await t["revise_proposal"]({"proposal_id": p["id"], "fields": {"example": "Phones are ubiquitous."}})
        return "Updated."

    llm.script.append(revise)
    await app.send_actions(UID, await agent.on_user_message(UID, "add an example", NOW))
    assert app.bot.deleted == [p["message_id"]]
    assert store.proposal(p["id"])["message_id"] != p["message_id"]


async def test_set_next_card_time_withdraws_question_and_ticker_asks_then(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    assert store.state(UID)["active_card_id"]

    async def later(prompt, t):
        assert "TIMER: waiting for the answer" in prompt
        out = await t["set_next_card_time"]({"minutes": 10})
        assert "12:10" in out  # Berlin time
        return "Sure, in 10 minutes."

    llm.script.append(later)
    await agent.on_user_message(UID, "ask me in 10 minutes", NOW)
    assert store.state(UID)["active_card_id"] is None

    llm.script.append(lambda p, t: _ret("Not yet"))
    await app.tick(NOW + timedelta(minutes=5))
    assert app.bot.sent == []
    llm.script.clear()

    async def ask(prompt, t):
        assert "ACTIVE CARD (asked" in prompt
        return "Time's up: what does ubiquitous mean?"

    llm.script.append(ask)
    await app.tick(NOW + timedelta(minutes=11))
    assert app.bot.sent == ["Time's up: what does ubiquitous mean?"]


async def test_context_shows_when_the_next_card_comes(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    store.update_state(UID, next_ask_at="2026-10-07T10:40:00+00:00")
    llm.script.append(lambda p, t: _ret("ok"))
    await agent.on_user_message(UID, "when is the next one?", NOW)
    assert "TIMER: the code brings up the next card at about 12:40." in llm.prompts[-1]


async def test_next_card_waits_for_the_timer_unless_user_asked_now(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    store.update_state(UID, next_ask_at="2026-10-07T10:40:00+00:00")

    async def eager(prompt, t):
        out = await t["next_card"]({})
        # The user didn't ask about timing, so no clock time reaches the model.
        assert out.startswith("Not opened") and "12:40" not in out
        return "It comes later."

    llm.script.append(eager)
    await agent.on_user_message(UID, "whenever the timer is up, ask me", NOW)
    assert store.state(UID)["active_card_id"] is None

    async def now(prompt, t):
        out = await t["next_card"]({"user_asked_now": True})
        assert "Ask this card now" in out
        return "What does ubiquitous mean?"

    llm.script.append(now)
    await agent.on_user_message(UID, "give me a card right now", NOW)
    assert store.state(UID)["active_card_id"]


async def test_grade_result_keeps_timing_quiet_and_note_has_no_time(agent, llm, store):
    from macaw.bot import render

    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]
    out = {}

    async def grade(prompt, t):
        out["r"] = await t["grade_card"]({"card_id": card_id, "rating": "Good", "reason": "spot on"})
        return "Yes!"

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "everywhere", NOW)
    assert "comes back" not in out["r"] and not re.search(r"\d{1,2}:\d{2}", out["r"])
    note = next(a for a in actions if isinstance(a, RatingNote))
    text, _ = render.rating_note(store, note.log_id, NOW)
    assert text == "<i>I rated: <b>Good</b> · spot on</i>"


async def test_grade_result_gives_times_when_the_user_asked(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]
    out = {}

    async def grade(prompt, t):
        out["r"] = await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        return "Yes!"

    llm.script.append(grade)
    await agent.on_user_message(UID, "everywhere. when does it come back?", NOW)
    assert "this card comes back today at 12:10" in out["r"]


async def _deck_with(agent, llm, store, cards):
    async def make(prompt, t):
        await t["create_deck"]({"name": "English", "deck_type": "vocabulary"})
        return "ok"

    llm.script.append(make)
    await agent.on_user_message(UID, "make a deck", NOW)
    for word, meaning in cards:
        async def step(prompt, t, word=word, meaning=meaning):
            await t["propose_card"]({"deck": "English", "fields": {"word": word, "meaning": meaning}})
            return "ok"

        llm.script.append(step)
        actions = await agent.on_user_message(UID, f"add {word}", NOW)
        preview = next(a for a in actions if isinstance(a, Preview))
        agent.add_proposal(preview.proposal_id, UID)


async def test_wrong_answer_matching_another_deck_card_is_pointed_out(agent, llm, store):
    await _deck_with(
        agent,
        llm,
        store,
        [
            ("recumbent", "lying down; reclining"),
            ("mottled", "marked with spots or smears of colour"),
            ("ubiquitous", "present everywhere"),
        ],
    )
    recumbent = next(c for c in store.user_cards(UID) if "recumbent" in c["note_fields"])
    mottled = next(c for c in store.user_cards(UID) if "mottled" in c["note_fields"])
    store.update_state(UID, active_card_id=recumbent["id"], asked_at=NOW.isoformat())

    async def grade(prompt, t):
        assert '"mottled" = marked with spots' in prompt
        assert "ubiquitous\" =" not in prompt
        out = await t["grade_card"]({"card_id": recumbent["id"], "rating": "Again", "reason": "mixed up"})
        assert "mottled" not in out  # the matched card isn't asked next
        return "Not quite: that's mottled from your deck. Recumbent means lying down."

    llm.script.append(grade)
    await agent.on_user_message(UID, "It means to have spots of various kinds of colors", NOW)
    # Its word was just shown next to its meaning, so it isn't a fair question today.
    assert store.card(mottled["id"])["buried_until"] is not None


async def test_no_lookalike_hint_for_unrelated_answer(agent, llm, store):
    await _deck_with(agent, llm, store, [("recumbent", "lying down"), ("mottled", "marked with spots of colour")])
    recumbent = next(c for c in store.user_cards(UID) if "recumbent" in c["note_fields"])
    store.update_state(UID, active_card_id=recumbent["id"], asked_at=NOW.isoformat())

    async def grade(prompt, t):
        assert "matches (found by word overlap" not in prompt
        await t["grade_card"]({"card_id": recumbent["id"], "rating": "Good", "reason": "right"})
        return "Yes!"

    llm.script.append(grade)
    await agent.on_user_message(UID, "lying down", NOW)


async def test_card_brief_says_the_user_cannot_see_the_card(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    assert "the user can't see any of this card" in llm.prompts[-1]


async def test_paused_session_offers_a_next_card_button(agent, llm, store):
    from macaw.bot import render

    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    store.update_user(UID, cards_per_session=1)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        assert "Session done" in out
        return "Yes!"

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "everywhere", NOW)
    note = next(a for a in actions if isinstance(a, RatingNote))
    assert note.offer_next
    _, kb = render.rating_note(store, note.log_id, NOW, offer_next=True)
    assert render.has_next(kb)
    assert store.state(UID)["next_ask_at"] > "2026-10-07T10:00"  # the timer had it for later

    # Tapping the button counts as asking now, so the timer doesn't hold it back.
    async def ask(prompt, t):
        assert "tapped the Next card button" in prompt
        return "And serendipity?"

    llm.script.append(ask)
    actions = await agent.ask_now(UID, NOW)
    assert actions[0].text == "And serendipity?"
    assert store.state(UID)["active_card_id"] not in (None, card_id)


async def test_no_next_card_button_when_nothing_is_due(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        return "Yes!"

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "everywhere", NOW)
    assert not next(a for a in actions if isinstance(a, RatingNote)).offer_next
    actions = await agent.ask_now(UID, NOW)
    assert actions == [Text("Nothing else is due right now.")]


async def test_sentence_on_card_front_is_kept_apart_from_the_word(agent, llm, store):
    # Imported Anki cards often have an example sentence under the word on the front.
    await _make_deck_with_card(agent, llm, "abode\nThey lived in a humble abode.", "a home")
    llm.script.append(lambda p, t: _ret("What does abode mean?"))
    await agent.ask_next(UID, NOW)
    prompt = llm.prompts[-1]
    assert "Ask about: abode\n" in prompt
    assert "never shown to the user (don't refer to it): They lived in a humble abode." in prompt


async def test_card_the_model_asked_on_its_own_is_adopted_by_its_word(agent, llm, store):
    await _make_deck_with_card(agent, llm, "recumbent\nShe lay recumbent on the sofa.", "lying down")
    await _make_deck_with_card(agent, llm, "accolade\nShe won many accolades.", "an award or praise")
    llm.script.append(lambda p, t: _ret('What does "accolade" mean?'))  # the active card is recumbent
    await agent.ask_next(UID, NOW)
    accolade = next(c["id"] for c in store.user_cards(UID) if "accolade" in store.note(c["note_id"])["fields"])
    assert store.state(UID)["active_card_id"] != accolade

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": accolade, "rating": "Again"})
        assert out.startswith("Graded")
        return "An accolade is an award."

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "Honestly, idk", NOW + timedelta(minutes=1))
    assert any(isinstance(a, RatingNote) for a in actions)


async def test_model_can_grade_a_due_card_it_asked_instead_of_the_active_one(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    await _make_deck_with_card(agent, llm, "serendipity", "happy accident")
    llm.script.append(lambda p, t: _ret("Quick one!"))
    await agent.ask_next(UID, NOW)
    active = store.state(UID)["active_card_id"]
    other = next(c["id"] for c in store.user_cards(UID) if c["id"] != active)

    async def grade(prompt, t):
        out = await t["grade_card"]({"card_id": other, "rating": "Again"})
        assert out.startswith("Graded")
        return "No worries."

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "no idea", NOW + timedelta(minutes=1))
    assert any(isinstance(a, RatingNote) for a in actions)


# ---------- disputed grades ----------


async def _graded_hard(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    llm.script.append(lambda p, t: _ret("What does 'ubiquitous' mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Hard", "reason": "core idea—but not all"})
        return "Close—it means present everywhere."

    llm.script.append(grade)
    actions = await agent.on_user_message(UID, "it's everywhere", NOW + timedelta(minutes=1))
    return card_id, actions


async def test_dashes_are_replaced_in_replies_and_reasons(agent, llm, store):
    _, actions = await _graded_hard(agent, llm, store)
    assert actions[0].text == "Close, it means present everywhere."
    note = next(a for a in actions if isinstance(a, RatingNote))
    assert store.review(note.log_id)["reason"] == "core idea, but not all"


async def test_bot_changes_its_grade_when_the_user_disputes_it(agent, llm, store):
    card_id, actions = await _graded_hard(agent, llm, store)
    log_id = next(a for a in actions if isinstance(a, RatingNote)).log_id

    async def regrade(prompt, t):
        assert f"Last grade: card #{card_id} (ubiquitous) graded Hard by you" in prompt
        out = await t["change_grade"]({"card_id": card_id, "rating": "Good", "reason": "you had the meaning"})
        assert out.startswith("Changed from Hard to Good")
        return "Fair point, it's Good now."

    llm.script.append(regrade)
    actions = await agent.on_user_message(UID, "that was right though, why Hard?", NOW + timedelta(minutes=2))
    note = next(a for a in actions if isinstance(a, RatingNote))
    assert note.log_id == log_id and note.changed
    r = store.review(log_id)
    assert (r["rating"], r["source"], r["reason"]) == (3, "claude", "you had the meaning")
    assert store.last_review_id(card_id) == log_id  # changed in place, not graded twice


async def test_changed_grade_edits_the_grade_message(agent, llm, store):
    app = _app(store, agent)
    edited = []

    async def edit_message_text(**kw):
        edited.append(kw["message_id"])

    app.bot.edit_message_text = edit_message_text
    card_id, actions = await _graded_hard(agent, llm, store)
    await app.send_actions(UID, actions)
    log_id = next(a for a in actions if isinstance(a, RatingNote)).log_id
    mid = store.review(log_id)["message_id"]
    assert mid

    llm.script.append(lambda p, t: t["change_grade"]({"card_id": card_id, "rating": "Good", "reason": "ok"}))
    actions = await agent.on_user_message(UID, "unfair", NOW + timedelta(minutes=2))
    sent = len(app.bot.sent)
    await app.send_actions(UID, [a for a in actions if isinstance(a, RatingNote)])
    assert edited == [mid] and len(app.bot.sent) == sent


async def test_only_the_latest_grade_can_be_changed(agent, llm, store):
    card_id, _ = await _graded_hard(agent, llm, store)
    later = NOW + REGRADE_WINDOW + timedelta(minutes=5)

    async def regrade(prompt, t):
        assert "Last grade:" not in prompt
        with pytest.raises(ValueError):
            await t["change_grade"]({"card_id": card_id, "rating": "Good", "reason": "x"})
        return "ok"

    llm.script.append(regrade)
    await agent.on_user_message(UID, "hm", later)


# ---------- answers that keep falling short ----------


async def test_filler_missed_notes_count_as_nothing():
    from macaw.agent import clean_missed

    for s in ("", None, "none", "Nothing.", "n/a", "-", " complete "):
        assert clean_missed(s) == ""
    assert clean_missed("the skill sense") == "the skill sense"


async def test_a_gap_that_comes_back_is_noticed_and_taught(agent, llm, store):
    await _make_deck_with_card(agent, llm, word="dexterous", meaning="skillful, especially with the hands")
    llm.script.append(lambda p, t: _ret("What does 'dexterous' mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def first(prompt, t):
        assert "Earlier answers" not in prompt
        out = await t["grade_card"](
            {"card_id": card_id, "rating": "Good", "reason": "agile, yes", "missed": "the skill sense"}
        )
        assert "fallen short" not in out
        return "Right, and it's also about skill."

    llm.script.append(first)
    await agent.on_user_message(UID, "having agility", NOW + timedelta(minutes=1))
    assert store.answer_history(card_id)[-1]["missed"] == "the skill sense"

    later = srs.parse(store.card(card_id)["due"]) + timedelta(minutes=1)
    llm.script.append(lambda p, t: _ret("What does 'dexterous' mean?"))
    await agent.ask_next(UID, later)
    brief = llm.prompts[-1]
    assert "Earlier answers to this card" in brief and "Good, missed: the skill sense" in brief

    async def second(prompt, t):
        assert "Good, missed: the skill sense" in prompt
        out = await t["grade_card"](
            {"card_id": card_id, "rating": "Again", "reason": "agile again", "missed": "skill, again"}
        )
        assert "fallen short before" in out and "the skill sense" in out
        assert "Do NOT ask another card" in out
        return "That's the second time skill slipped. Put it in your own words?"

    llm.script.append(second)
    await agent.on_user_message(UID, "agile", later + timedelta(minutes=1))
    assert store.state(UID)["active_card_id"] is None

    # Saying the meaning back doesn't count as leaking the card's answer.
    back = srs.parse(store.card(card_id)["due"]) + timedelta(minutes=1)
    assert back < later + timedelta(minutes=30)
    llm.script.append(lambda p, t: _ret("Exactly."))
    await agent.on_user_message(UID, "skillful, especially with the hands", back)
    assert store.card(card_id)["buried_until"] is None


async def test_skipped_missed_field_falls_back_to_the_reason_on_hard(agent, llm, store):
    card_id, actions = await _graded_hard(agent, llm, store)
    log_id = next(a for a in actions if isinstance(a, RatingNote)).log_id
    assert store.review(log_id)["missed"] == "core idea, but not all"


# ---------- a card ignored for days ----------


def _numbered_replies(llm, n=400):
    for i in range(n):
        llm.script.append(lambda p, t, i=i: _ret(f"message {i}"))


async def _run_days(app, start, days, step=timedelta(minutes=15)):
    """Tick through `days` days; returns {study-day index: [texts sent]}."""
    sent: dict[int, list[str]] = {}
    t = start
    user = dict(app.store.get_user(UID))
    while t < start + timedelta(days=days):
        before = len(app.bot.sent)
        await app.tick(t)
        for text in app.bot.sent[before:]:
            sent.setdefault(srs.study_days_between(user, start, t), []).append(text)
        t += step
    return sent


async def test_ignored_card_gets_one_message_a_day_and_never_stops(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    _numbered_replies(llm)
    sent = await _run_days(app, NOW + timedelta(minutes=5), 40)
    # The day it is asked: the question plus the usual reminders.
    assert len(sent[0]) >= 3
    # After that: never more than one a day; the first two days always get one.
    assert all(len(v) == 1 for k, v in sent.items() if k >= 1)
    assert 1 in sent and 2 in sent
    quiet_days = [d for d in range(1, 40) if d not in sent]
    assert quiet_days  # it skips some days
    assert not any(d + 1 in quiet_days and d + 2 in quiet_days for d in quiet_days)
    assert max(sent) >= 38  # still writing after more than a month

    nags = [p for p in llm.prompts if "You write at most once a day now" in p]
    assert "no disappointment" in nags[0]  # missing one day is fine
    assert "disappointment" in nags[3] and "no disappointment" not in nags[3]
    assert "wistful" in nags[-1]
    # It sees what it said before so it can say something new.
    assert "message" in nags[-1].split("got no reply")[1]
    # No message in quiet hours (Berlin 00:00-08:00 is 22:00-06:00 UTC).
    assert store.state(UID)["active_card_id"] is not None


async def test_reply_asking_for_a_month_is_honored_then_checks_in(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    _numbered_replies(llm, 30)
    start = NOW + timedelta(minutes=5)
    await _run_days(app, start, 7)
    llm.script.clear()

    async def later(prompt, t):
        assert "Ignored: no card has been done for" in prompt
        out = await t["give_space"]({"days": 30})
        assert "silent" in out or "nothing" in out
        return "Fine. A month it is."

    llm.script.append(later)
    talk = start + timedelta(days=7, hours=1)
    await agent.on_user_message(UID, "come back in a month", talk)
    assert store.state(UID)["active_card_id"] is None
    _numbered_replies(llm, 30)
    n = len(app.bot.sent)
    silent = await _run_days(app, talk, 29)
    assert silent == {} and len(app.bot.sent) == n
    after = await _run_days(app, talk + timedelta(days=29), 3)
    assert sum(len(v) for v in after.values()) >= 1
    check_ins = [p for p in llm.prompts if "That time is up now" in p]
    assert len(check_ins) == 1
    assert store.state(UID)["space_until"] is None
    assert store.state(UID)["active_card_id"] is not None


async def test_stop_writing_stops_everything_until_a_card_is_done(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    _numbered_replies(llm, 30)
    start = NOW + timedelta(minutes=5)
    await _run_days(app, start, 3)
    llm.script.clear()

    async def stop(prompt, t):
        await t["stop_writing"]({})
        return "Okay. I'll be here."

    llm.script.append(stop)
    talk = start + timedelta(days=3, hours=1)
    await agent.on_user_message(UID, "stop writing to me", talk)
    n = len(app.bot.sent)
    _numbered_replies(llm, 30)
    assert await _run_days(app, talk, 20) == {}
    assert len(app.bot.sent) == n

    # They come back and do a card on their own: back to normal.
    llm.script.clear()
    back = talk + timedelta(days=20, hours=2)
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_now(UID, back)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        return "Yes!"

    llm.script.append(grade)
    await agent.on_user_message(UID, "everywhere", back + timedelta(minutes=1))
    st = store.state(UID)
    assert st["contact_off"] == 0 and st["ignored_since"] is None


async def test_writing_on_an_ignored_day_brings_back_normal_timing(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    app = _app(store, agent)
    _numbered_replies(llm, 60)
    start = NOW + timedelta(minutes=5)
    await _run_days(app, start, 4)
    llm.script.clear()
    llm.script.append(lambda p, t: _ret("Oh, hello."))
    # 08:30 Berlin on day 4: the user writes but doesn't answer.
    talk = at("2026-10-11 06:30")
    await agent.on_user_message(UID, "hey", talk)
    _numbered_replies(llm, 20)
    n = len(app.bot.sent)
    await _run_days(app, talk, 0.6)
    # Usual reminders that day, counted from their message, rather than one dry nag.
    assert len(app.bot.sent) - n >= 2
    assert not any("You write at most once a day now" in p for p in llm.prompts[-2:])


async def test_context_counts_what_was_done_today(agent, llm, store):
    await _make_deck_with_card(agent, llm)
    # Notes get the real clock as created_at; move this one into the test's study day.
    store.x("UPDATE notes SET created_at=?", (iso(NOW - timedelta(minutes=5)),))
    store.x("INSERT INTO notes(deck_id, fields, sort_key, created_at) VALUES (?,?,?,?)",
                        (store.deck_by_name(UID, "English")["id"], '{"word": "old", "meaning": "x"}', "old",
                         iso(NOW - timedelta(days=1))))
    llm.script.append(lambda p, t: _ret("What does ubiquitous mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def grade(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Again", "reason": "missed"})
        return "Not quite."

    llm.script.append(grade)
    await agent.on_user_message(UID, "no idea", NOW)
    llm.script.append(lambda p, t: _ret("One so far."))
    await agent.on_user_message(UID, "how many cards did we do today?", NOW + timedelta(minutes=1))
    prompt = llm.prompts[-1]
    assert "study day started 08:00" in prompt
    assert "1 answer graded (1 Again) on 1 different card (1 of them seen for the first time today): ubiquitous" in prompt
    assert "New notes: 1 added (ubiquitous)" in prompt


async def test_today_count_resets_when_quiet_hours_end(agent, llm, store, user):
    await _make_deck_with_card(agent, llm)
    card = store.user_cards(UID)[0]
    late = at("2026-10-07 23:30")  # 01:30 in Berlin, still the 7th's study day
    srs.grade(store, user, card["id"], 3, "user", when=late)
    assert srs.done_today(store, user, at("2026-10-08 05:00"))["reviews"] == 1  # 07:00 Berlin
    assert srs.done_today(store, user, at("2026-10-08 06:30"))["reviews"] == 0  # 08:30 Berlin


async def test_a_plain_miss_then_a_small_gap_is_not_a_gap_that_keeps_coming_back(agent, llm, store):
    await _make_deck_with_card(agent, llm, word="exult", meaning="to feel or show great joy after a success")
    llm.script.append(lambda p, t: _ret("What does 'exult' mean?"))
    await agent.ask_next(UID, NOW)
    card_id = store.state(UID)["active_card_id"]

    async def wrong(prompt, t):
        await t["grade_card"]({"card_id": card_id, "rating": "Again", "reason": "other word", "missed": "all of it"})
        return "Not quite."

    llm.script.append(wrong)
    await agent.on_user_message(UID, "to exclude", NOW + timedelta(minutes=1))
    later = srs.parse(store.card(card_id)["due"]) + timedelta(minutes=1)
    llm.script.append(lambda p, t: _ret("Back to exult?"))
    await agent.ask_next(UID, later)

    async def right(prompt, t):
        out = await t["grade_card"](
            {"card_id": card_id, "rating": "Good", "reason": "joy", "missed": "the success part"}
        )
        assert "fallen short before" not in out
        return "Yes."

    llm.script.append(right)
    await agent.on_user_message(UID, "really happy", later + timedelta(minutes=1))
