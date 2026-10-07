"""Language model providers. The bot talks to one `LLMProvider`; Claude via the
Pro subscription is the default, and others can be added beside it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema for the arguments
    handler: Callable[[dict[str, Any]], Awaitable[str]]


class LLMError(Exception):
    """The model could not be reached (usage limit, network, auth)."""


class LLMProvider(Protocol):
    async def run(self, system: str, prompt: str, tools: list[ToolSpec]) -> str:
        """Run one turn: the model may call tools, then returns its final text."""
        ...


def make_provider(name: str, model: str | None = None) -> LLMProvider:
    if name == "claude":
        from .claude import ClaudeCodeProvider

        return ClaudeCodeProvider(model=model)
    raise SystemExit(f"Unknown LLM_PROVIDER {name!r}")
