"""Claude speed settings: model, thinking effort, and keeping the next Claude Code process ready."""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import ResultMessage

from macaw.llm import ClaudeOptions, ToolSpec, claude_options, options_for
from macaw.llm import claude as claude_mod
from macaw.llm.claude import ClaudeCodeProvider


class FakeClient:
    started: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.connected = False
        self.closed = False
        self.queries: list[str] = []
        FakeClient.started.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.closed = True

    async def query(self, prompt):
        assert self.connected and not self.closed
        self.queries.append(prompt)

    async def receive_response(self):
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", usage={}, result="hi",
        )


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    FakeClient.started = []
    monkeypatch.setattr(claude_mod, "ClaudeSDKClient", FakeClient)


async def noop(args):
    return "ok"


TOOLS = [ToolSpec("t", "a tool", {"type": "object", "properties": {}}, noop)]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_next_process_is_started_in_advance_and_used():
    p = ClaudeCodeProvider(model="claude-opus-5-5")
    claude_options.set(ClaudeOptions())
    assert await p.run("sys", "one", TOOLS) == "hi"
    await settle()
    assert len(FakeClient.started) == 2  # the used one and the one waiting
    first, waiting = FakeClient.started
    assert first.closed and waiting.connected and not waiting.closed
    assert await p.run("sys", "two", TOOLS) == "hi"
    assert waiting.queries == ["two"]


async def test_keep_ready_off_starts_fresh_and_closes_waiting():
    p = ClaudeCodeProvider()
    claude_options.set(ClaudeOptions())
    await p.run("sys", "one", TOOLS)
    await settle()
    waiting = FakeClient.started[1]
    claude_options.set(ClaudeOptions(warm=False))
    await p.run("sys", "two", TOOLS)
    await settle()
    assert waiting.closed and waiting.queries == []
    assert len(FakeClient.started) == 3


async def test_model_and_effort_reach_claude_code():
    p = ClaudeCodeProvider(model="claude-opus-5-5")
    claude_options.set(ClaudeOptions(model="sonnet", effort="low", warm=False))
    await p.run("sys", "x", TOOLS)
    assert FakeClient.started[-1].options.model == "sonnet"
    assert FakeClient.started[-1].options.effort == "low"
    claude_options.set(ClaudeOptions(model="opus", effort="medium", warm=False))
    await p.run("sys", "x", TOOLS)
    assert FakeClient.started[-1].options.model == "claude-opus-5-5"  # the .env model when it is Opus
    claude_options.set(ClaudeOptions(model="haiku", effort="low", warm=False))
    await p.run("sys", "x", TOOLS)
    assert FakeClient.started[-1].options.effort is None  # Haiku takes no effort level


def test_options_from_user_row():
    assert options_for({}) == ClaudeOptions()
    assert options_for({"claude_model": "haiku", "claude_effort": "low", "claude_warm": 0}) == ClaudeOptions(
        "haiku", "low", False
    )
    assert options_for({"claude_model": "gpt", "claude_effort": "max"}) == ClaudeOptions()
