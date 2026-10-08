from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macaw.db import Store  # noqa: E402
from macaw.llm import ToolSpec  # noqa: E402

UID = 42


class FakeLLM:
    """Scripted stand-in for Claude. Each script step is a function that gets the
    prompt and a dict of tool handlers, may call tools, and returns the reply."""

    def __init__(self) -> None:
        self.script: list[Callable[[str, dict[str, Callable[[dict], Awaitable[str]]]], Awaitable[str]]] = []
        self.prompts: list[str] = []
        self.tool_results: list[str] = []
        self.must_use_tool: list[bool] = []

    async def run(self, system: str, prompt: str, tools: list[ToolSpec], must_use_tool: bool = False) -> str:
        self.prompts.append(prompt)
        self.must_use_tool.append(must_use_tool)
        handlers = {}
        for t in tools:
            async def call(args: dict[str, Any], t=t) -> str:
                out = await t.handler(args)
                self.tool_results.append(out)
                return out

            handlers[t.name] = call
        step = self.script.pop(0) if self.script else None
        return await step(prompt, handlers) if step else "ok"


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.ensure_user(UID, UID, "Europe/Berlin")
    return s


@pytest.fixture
def user(store: Store) -> dict:
    return dict(store.get_user(UID))


def at(s: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' as UTC."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
