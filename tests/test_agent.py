from __future__ import annotations

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

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

        class M:
            message_id = len(self.sent)

        return M()

    async def send_chat_action(self, *a, **kw):
        pass


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
