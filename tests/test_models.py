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

    async def run(self, system, prompt, tools):
        await super().run(system, prompt, tools)
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
