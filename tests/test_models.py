from __future__ import annotations

import json

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

    async def fake_post(self, session, body):
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

    async def fake_post(self, session, body):
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

    async def fake_post(self, session, body):
        raise oc.RateLimited(500)

    monkeypatch.setattr(OpenAICompatProvider, "_post", fake_post)
    p = OpenAICompatProvider("https://x.test/v1", "k", ["a", "b"])
    with pytest.raises(LLMError):
        await p.run("s", "p", [])


def test_gpt_oss_gets_low_reasoning_effort():
    from macaw.llm.openai_compat import _body_for

    assert _body_for("openai/gpt-oss-20b", {})["reasoning_effort"] == "low"
    assert "reasoning_effort" not in _body_for("qwen/qwen3.8-27b", {})


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

    async def fake_post(self, session, body):
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
