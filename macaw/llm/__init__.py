"""Language model providers. Each turn runs on one `LLMProvider`: Claude via the
Pro subscription for owners, or the free model (any OpenAI-style API, Groq by
default) for guests and for owners who switch to it."""

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
    async def run(self, system: str, prompt: str, tools: list[ToolSpec], must_use_tool: bool = False) -> str:
        """Run one turn: the model may call tools, then returns its final text.

        `must_use_tool` asks the model to call at least one tool before replying.
        Providers whose models follow the system prompt reliably may ignore it."""
        ...


def make_provider(name: str, model: str | None = None) -> LLMProvider:
    if name == "claude":
        from .claude import ClaudeCodeProvider

        return ClaudeCodeProvider(model=model)
    raise SystemExit(f"Unknown LLM_PROVIDER {name!r}")


CLAUDE = "claude"
FREE = "free"


class Models:
    """Picks the model for each user. Only owners may ever use Claude (it runs on
    the owner's own Pro subscription); guests always get the free model."""

    def __init__(self, owner_ids: frozenset[int], claude: LLMProvider, free: LLMProvider | None):
        self.owner_ids = owner_ids
        self.claude = claude
        self.free = free

    def name_for(self, user: dict[str, Any]) -> str:
        if user["id"] in self.owner_ids and user.get("llm") != FREE:
            return CLAUDE
        return FREE

    def for_user(self, user: dict[str, Any]) -> LLMProvider:
        if self.name_for(user) == CLAUDE:
            return self.claude
        if self.free is None:
            raise LLMError("the free model is not set up (FREE_LLM_API_KEY is empty)")
        return self.free


def make_free_provider(
    api_key: str | None, base_url: str, model: str, fallbacks: tuple[str, ...] = ()
) -> LLMProvider | None:
    if not api_key:
        return None
    from .openai_compat import OpenAICompatProvider

    models = [model] + [m for m in fallbacks if m != model]
    return OpenAICompatProvider(base_url, api_key, models)
