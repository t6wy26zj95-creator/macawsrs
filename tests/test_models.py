from __future__ import annotations

import json
import re

import pytest

from macaw.agent import Agent, Text
from macaw.config import Config
from macaw.llm import CLAUDE, FREE, LLMError, Models, ToolSpec
from macaw.llm.openai_compat import OpenAICompatProvider

from .conftest import UID, FakeLLM, at

GUEST = 77
NOW = at("2026-10-07 10:00")


class Named(FakeLLM):
    def __init__(self, name: str):
        super().__init__()
        self.name = name

    async def run(self, system, prompt, tools, must_use_tool=False):
        await super().run(system, prompt, tools, must_use_tool)
        return f"from {self.name}"


@pytest.fixture
def models():
    return Models(frozenset({UID}), Named("claude"), Named("free"))


def test_owner_defaults_to_claude_and_can_switch(models, store):
    assert models.name_for(dict(store.get_user(UID))) == CLAUDE
    store.update_user(UID, llm=FREE)
    assert models.name_for(dict(store.get_user(UID))) == FREE
    store.update_user(UID, llm=CLAUDE)
    assert models.name_for(dict(store.get_user(UID))) == CLAUDE


def test_guest_never_gets_claude(models, store):
    store.ensure_user(GUEST, GUEST, "UTC")
    store.update_user(GUEST, llm=CLAUDE)  # even if the column were tampered with
    assert models.name_for(dict(store.get_user(GUEST))) == FREE
    assert models.for_user(dict(store.get_user(GUEST))) is models.free


def test_guest_without_free_key_errors_instead_of_using_claude(store):
    m = Models(frozenset({UID}), Named("claude"), None)
    store.ensure_user(GUEST, GUEST, "UTC")
    with pytest.raises(LLMError):
        m.for_user(dict(store.get_user(GUEST)))


async def test_agent_routes_each_user_to_their_model(models, store):
    agent = Agent(store, models.claude, models)
    store.ensure_user(GUEST, GUEST, "UTC")
    assert (await agent.on_user_message(UID, "hi", NOW))[0] == Text("from claude")
    assert (await agent.on_user_message(GUEST, "hi", NOW))[0] == Text("from free")
    store.update_user(UID, llm=FREE)
    assert (await agent.on_user_message(UID, "hi again", NOW))[0] == Text("from free")
    # The switch keeps the conversation: the earlier exchange is in the new model's context.
    assert "from claude" in models.free.prompts[-1]


def test_guest_ids_never_overlap_owners(monkeypatch):
    from macaw.config import load_config

    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_IDS", "1")
    monkeypatch.setenv("GUEST_IDS", "1, 2")
    cfg = load_config()
    assert cfg.guest_ids == frozenset({2})
    assert cfg.allowed_ids == frozenset({1, 2})


async def test_openai_compat_runs_tool_rounds(monkeypatch):
    calls = []

    async def handler(args):
        calls.append(args)
        return "added"

    tool = ToolSpec("propose_card", "d", {"type": "object", "properties": {}}, handler)
    replies = [
        {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "propose_card", "arguments": json.dumps({"deck": "E"})}},
            {"id": "c2", "type": "function", "function": {"name": "nope", "arguments": "{}"}},
        ]}}]},
        {"choices": [{"message": {"content": " Done. "}}]},
    ]
    bodies = []

    async def fake_post(self, session, slot, body):
        bodies.append(json.loads(json.dumps(body)))
        return replies.pop(0)

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    p = OpenAICompatProvider("https://example.test/v1", "k", "m")
    assert await p.run("sys", "prompt", [tool]) == "Done."
    assert calls == [{"deck": "E"}]
    tool_msgs = [m for m in bodies[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "added"
    assert tool_msgs[1]["content"].startswith("Error: unknown tool")
    assert bodies[0]["tools"][0]["function"]["name"] == "propose_card"


def test_config_defaults_keep_old_positional_construction():
    cfg = Config("t", frozenset({UID}), None, "UTC", "fake", None, "INFO")
    assert cfg.allowed_ids == frozenset({UID}) and cfg.free_api_key is None


def test_invite_link_works_once_and_expires(store):
    store.create_invite("abc", UID, at("2026-10-14 10:00"))
    store.create_invite("old", UID, at("2026-10-01 10:00"))
    assert store.redeem_invite("old", GUEST, "Ann", NOW) is None
    assert store.redeem_invite("nope", GUEST, "Ann", NOW) is None
    assert not store.is_guest(GUEST)
    assert store.redeem_invite("abc", GUEST, "Ann", NOW) == UID
    assert store.is_guest(GUEST)
    assert store.redeem_invite("abc", 88, "Bob", NOW) is None  # one person only
    assert [g["name"] for g in store.guests()] == ["Ann"]
    assert store.remove_guest(GUEST) and not store.is_guest(GUEST)


def test_invited_guest_gets_free_model(models, store):
    store.create_invite("abc", UID, at("2026-10-14 10:00"))
    store.redeem_invite("abc", GUEST, "Ann", NOW)
    store.ensure_user(GUEST, GUEST, "UTC")
    assert models.for_user(dict(store.get_user(GUEST))) is models.free


def test_prune_keeps_each_users_history(store):
    for i in range(5):
        store.log_message(UID, "user", f"a{i}")
    store.log_message(GUEST, "user", "only one")
    for i in range(5):
        store.log_message(UID, "user", f"b{i}")
    store.prune_messages(keep=3)
    assert [r["text"] for r in store.recent_messages(GUEST)] == ["only one"]
    assert [r["text"] for r in store.recent_messages(UID)] == ["b2", "b3", "b4"]


async def test_rate_limit_moves_to_next_model_then_waits(monkeypatch):
    from macaw.llm import openai_compat as oc

    seen = []
    limited = {"a": 2, "b": 1}  # how many more times each model answers 429

    async def fake_post(self, session, slot, body):
        seen.append(body["model"])
        if limited[body["model"]] > 0:
            limited[body["model"]] -= 1
            raise oc.RateLimited(0.01)
        return {"choices": [{"message": {"content": "hi from " + body["model"]}}]}

    async def no_sleep(s):
        pass

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    monkeypatch.setattr(oc.asyncio, "sleep", no_sleep)
    # The clock jumps forward once, as if the provider had slept.
    times = iter([1000.0] * 5)
    monkeypatch.setattr(oc.time, "monotonic", lambda: next(times, 2000.0))
    p = OpenAICompatProvider("https://x.test/v1", "k", ["a", "b"])

    # a and b both limited: the provider waits, then a is limited again, b answers.
    assert await p.run("s", "p", []) == "hi from b"
    assert seen == ["a", "b", "a", "b"]


async def test_gives_up_when_wait_is_too_long(monkeypatch):
    from macaw.llm import openai_compat as oc

    async def fake_post(self, session, slot, body):
        raise oc.RateLimited(500)

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    p = OpenAICompatProvider("https://x.test/v1", "k", ["a", "b"])
    with pytest.raises(LLMError):
        await p.run("s", "p", [])


def test_gpt_oss_gets_low_reasoning_effort():
    from macaw.llm.openai_compat import Slot, _body_for

    assert _body_for(Slot("https://api.groq.com/openai/v1", "k", "openai/gpt-oss-20b"), {})["reasoning_effort"] == "low"
    assert "reasoning_effort" not in _body_for(Slot("https://api.groq.com/openai/v1", "k", "qwen/qwen3.8-27b"), {})


async def test_answering_an_active_card_requires_a_tool(store):
    from macaw.agent import Agent
    from .test_agent import _make_deck_with_card

    llm = FakeLLM()
    agent = Agent(store, llm)
    await _make_deck_with_card(agent, llm)
    await agent.on_user_message(UID, "hi", NOW)
    assert llm.must_use_tool[-1] is False  # no open card: free chat

    llm.script.append(lambda p, t: _ret("What does 'ubiquitous' mean?"))
    await agent.ask_next(UID, NOW)

    seen = {}

    async def step(prompt, t):
        seen["prompt"] = prompt
        seen["tools"] = set(t)
        return await t["not_an_answer"]({})

    llm.script.append(step)
    await agent.on_user_message(UID, "hey that was abrupt", NOW)
    assert llm.must_use_tool[-1] is True
    assert "not_an_answer" in seen["tools"] and "call grade_card before you reply" in seen["prompt"]
    assert store.state(UID)["active_card_id"]  # still open


async def test_openai_compat_requires_tool_only_on_first_round(monkeypatch):
    bodies = []
    replies = [
        {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "ok"}}]},
    ]

    async def fake_post(self, session, slot, body):
        bodies.append(body)
        return replies.pop(0)

    async def handler(args):
        return "done"

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    p = OpenAICompatProvider("https://x.test/v1", "k", "m")
    await p.run("s", "p", [ToolSpec("t", "d", {"type": "object", "properties": {}}, handler)], must_use_tool=True)
    assert bodies[0]["tool_choice"] == "required" and "tool_choice" not in bodies[1]


async def _ret(text):
    return text


async def test_card_the_model_asked_itself_is_graded_not_postponed(store):
    from macaw.agent import Agent
    from .test_agent import _make_deck_with_card

    llm = FakeLLM()
    agent = Agent(store, llm)
    await _make_deck_with_card(agent, llm)
    assert not store.state(UID)["active_card_id"]
    # The model quizzes the card on its own, without the timer making it active.
    store.log_message(UID, "bot", "Quick one: what does 'ubiquitous' mean?")

    seen = {}

    async def grade(prompt, t):
        cid = store.state(UID)["active_card_id"]
        seen["cid"] = cid
        await t["grade_card"]({"card_id": cid, "rating": "Good"})
        return "Exactly."

    llm.script.append(grade)
    await agent.on_user_message(UID, "present everywhere", NOW)
    assert seen["cid"] and llm.must_use_tool[-1] is True
    assert "postponed" not in " ".join(r["text"] for r in store.recent_messages(UID, 20))


async def test_mentioning_a_word_without_asking_does_not_open_it(store):
    from macaw.agent import Agent
    from .test_agent import _make_deck_with_card

    llm = FakeLLM()
    agent = Agent(store, llm)
    await _make_deck_with_card(agent, llm)
    store.log_message(UID, "bot", "Added ubiquitous to your deck.")
    await agent.on_user_message(UID, "thanks", NOW)
    assert not store.state(UID)["active_card_id"]


async def test_times_reach_the_model_only_when_asked(store):
    from macaw.agent import Agent
    from .test_agent import _make_deck_with_card

    llm = FakeLLM()
    agent = Agent(store, llm)
    await _make_deck_with_card(agent, llm)
    clock = re.compile(r"TIMER:[^\n]*\d{1,2}:\d{2}")
    await agent.on_user_message(UID, "I had a good day", NOW)
    assert not clock.search(llm.prompts[-1])
    await agent.on_user_message(UID, "when is the next card?", NOW)
    assert clock.search(llm.prompts[-1])


def test_free_provider_puts_gemini_before_groq():
    from macaw.llm import make_free_provider

    p = make_free_provider("gsk", "https://api.groq.com/openai/v1", "a", ("b",), "gem", ("g1", "g2"))
    assert [s.model for s in p.slots] == ["g1", "g2", "a", "b"]
    assert p.slots[0].is_gemini and not p.slots[2].is_gemini
    assert make_free_provider(None, "x", "a", (), None, ("g1",)) is None
    assert [s.model for s in make_free_provider(None, "x", "a", (), "gem", ("g1",)).slots] == ["g1"]


def test_gemini_body_drops_additional_properties():
    from macaw.llm.openai_compat import Slot, _body_for

    tool = {"type": "function", "function": {"name": "t", "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {"x": {"type": "object", "additionalProperties": False}}}}}
    body = _body_for(Slot("https://generativelanguage.googleapis.com/v1beta/openai/", "k", "g"), {"tools": [tool]})
    assert "additionalProperties" not in json.dumps(body)
    assert body["reasoning_effort"] == "low"
    assert tool["function"]["parameters"]["additionalProperties"] is False  # original untouched


async def test_broken_model_falls_through_and_tool_choice_retried(monkeypatch):
    from macaw.llm import openai_compat as oc

    calls = []

    async def fake_post(self, session, slot, body):
        calls.append((slot.model, "tool_choice" in body))
        if slot.model == "dead":
            raise oc.HTTPError(404, "dead: not found")
        if "tool_choice" in body:
            raise oc.HTTPError(400, "bad tool_choice")
        return {"choices": [{"message": {"content": "hi from " + slot.model}}]}

    async def handler(args):
        return "x"

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    p = OpenAICompatProvider(slots=[oc.Slot("u", "k", "dead"), oc.Slot("u", "k", "ok")])
    tools = [ToolSpec("t", "d", {"type": "object", "properties": {}}, handler)]
    assert await p.run("s", "p", tools, must_use_tool=True) == "hi from ok"
    assert calls == [("dead", True), ("ok", True), ("ok", False)]
    # The dead one is skipped for a while on the next turn.
    calls.clear()
    assert await p.run("s", "p", tools) == "hi from ok"
    assert calls == [("ok", False)]


def test_free_provider_order_with_nvidia():
    from macaw.llm import make_free_provider

    p = make_free_provider("gsk", "https://api.groq.com/openai/v1", "a", (), "gem", ("g1",), "nv", ("n1", "n2"))
    assert [s.model for s in p.slots] == ["g1", "n1", "n2", "a"]
    assert "nvidia" in p.slots[1].base_url


def test_free_label_and_default_models(monkeypatch):
    from macaw.config import GEMINI_MODELS, load_config

    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_IDS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", "n")
    monkeypatch.setenv("FREE_LLM_API_KEY", "q")
    monkeypatch.setenv("GEMINI_MODELS", "")
    cfg = load_config()
    assert cfg.free_label == "Gemini, with NVIDIA and Groq as backup"
    assert cfg.gemini_models == GEMINI_MODELS and any("lite" in m for m in GEMINI_MODELS)


async def test_daily_limit_parks_the_model_until_reset(monkeypatch):
    from macaw.llm import openai_compat as oc

    calls = []

    async def fake_post(self, session, slot, body):
        calls.append(slot.model)
        if slot.model == "flash":
            raise oc.RateLimited(oc._daily_wait(slot), daily=True)
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    gem = oc.Slot("https://generativelanguage.googleapis.com/v1beta/openai/", "k", "flash")
    p = OpenAICompatProvider(slots=[gem, oc.Slot("u", "k", "lite")])
    assert await p.run("s", "p", []) == "hi"
    assert await p.run("s", "p", []) == "hi"
    assert calls == ["flash", "lite", "lite"]  # not asked again on the second turn
    assert p.free_at[gem.key] - oc.time.monotonic() > 60


def test_daily_429_detection_and_reset_time():
    from datetime import datetime, timezone

    from macaw.llm import openai_compat as oc

    assert oc._is_daily('{"error": {"message": "Quota exceeded for metric: '
                        'generate_requests_per_model_per_day, quotaId: GenerateRequestsPerDayPerProjectPerModel"}}')
    assert oc._is_daily("Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): Limit 200000")
    assert not oc._is_daily("Rate limit reached on tokens per minute (TPM): Limit 8000")
    gem = oc.Slot("https://generativelanguage.googleapis.com/v1beta/openai/", "k", "g")
    # 07:00 UTC is midnight in Los Angeles (PDT), so the reset is almost a day away.
    wait = oc._daily_wait(gem, datetime(2026, 10, 11, 7, 30, tzinfo=timezone.utc))
    assert 23 * 3600 < wait < 24 * 3600
    assert oc._daily_wait(oc.Slot("u", "k", "m")) == oc.DAILY_FALLBACK


async def test_missing_model_is_skipped_for_a_day_and_reasoning_effort_retried(monkeypatch):
    from macaw.llm import openai_compat as oc

    calls = []

    async def fake_post(self, session, slot, body):
        calls.append((slot.model, "reasoning_effort" in body))
        if slot.model == "openai/gpt-oss-gone":
            raise oc.HTTPError(404, "not found")
        if "reasoning_effort" in body:
            raise oc.HTTPError(400, "unknown field reasoning_effort")
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    gone = oc.Slot("u", "k", "openai/gpt-oss-gone")
    p = OpenAICompatProvider(slots=[gone, oc.Slot("u", "k", "openai/gpt-oss-ok")])
    assert await p.run("s", "p", []) == "hi"
    assert calls == [("openai/gpt-oss-gone", True), ("openai/gpt-oss-ok", True), ("openai/gpt-oss-ok", False)]
    assert p.free_at[gone.key] - oc.time.monotonic() > 3600


class Failing(FakeLLM):
    def __init__(self, tool: str | None = None):
        super().__init__()
        self.tool = tool
        self.ok = False

    async def run(self, system, prompt, tools, must_use_tool=False):
        self.prompts.append(prompt)
        if self.ok:
            return "from claude"
        if self.tool:
            await next(t for t in tools if t.name == self.tool).handler({})
        raise LLMError("Claude AI usage limit reached")


async def test_free_model_stands_in_while_claude_is_down():
    from macaw.llm import ClaudeWithBackup

    claude, free = Failing(), Named("free")
    m = Models(frozenset({UID}), claude, free)
    llm = m.for_user({"id": UID})
    assert isinstance(llm, ClaudeWithBackup)
    assert await llm.run("s", "p", [], must_use_tool=True) == "from free"
    assert free.must_use_tool == [True]
    assert m.pop_notice() and not m.pop_notice()  # told once
    # While it's down, Claude isn't tried at all.
    claude.ok = True
    assert await llm.run("s", "p", []) == "from free" and len(claude.prompts) == 1
    # After the pause Claude is tried again and takes back over.
    llm.down_until = 0
    assert await llm.run("s", "p", []) == "from claude"
    assert llm.pause == 0 and not m.pop_notice()


async def test_no_stand_in_once_claude_ran_a_tool():
    async def handler(args):
        return "graded"

    m = Models(frozenset({UID}), Failing(tool="grade_card"), Named("free"))
    with pytest.raises(LLMError):
        await m.for_user({"id": UID}).run("s", "p", [ToolSpec("grade_card", "d", {}, handler)])
    assert m.free.prompts == []


def test_cli_error_text_is_an_error_not_a_reply():
    from macaw.llm.claude import CLI_ERROR

    assert CLI_ERROR.match("Claude AI usage limit reached|1760000000")
    assert CLI_ERROR.match("Invalid API key · Please run /login")
    assert not CLI_ERROR.match("What does 'subscription' mean?")


def test_old_example_gemini_models_get_the_new_defaults(monkeypatch):
    from macaw.config import GEMINI_MODELS, load_config

    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_IDS", "1")
    monkeypatch.setenv("GEMINI_MODELS", "gemini-3.8-flash,gemini-2.5-flash")
    assert load_config().gemini_models == GEMINI_MODELS
    monkeypatch.setenv("GEMINI_MODELS", "gemini-x")
    assert load_config().gemini_models == ("gemini-x",)
