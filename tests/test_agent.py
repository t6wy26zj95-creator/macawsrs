from __future__ import annotations

import re

from datetime import timedelta

import pytest

from macaw.agent import Agent, ConfirmDelete, Preview, RatingNote, Text
from macaw.bot.app import App
from macaw.config import Config

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
