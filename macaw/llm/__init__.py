"""Language model providers. Each turn runs on one `LLMProvider`: Claude via the
Pro subscription for owners, or the free model (Gemini and/or Groq, through
their OpenAI-style APIs) for guests and for owners who switch to it."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)


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

# Owner speed settings for Claude, chosen in /settings.
CLAUDE_MODELS = ("opus", "sonnet", "haiku")
EFFORTS = ("high", "medium", "low")


@dataclass(frozen=True)
class ClaudeOptions:
    model: str | None = None  # "opus", "sonnet", "haiku"; None = CLAUDE_MODEL from .env
    effort: str | None = None  # "high", "medium", "low"; None = Claude's default (high)
    warm: bool = True  # keep the next Claude Code process started and waiting
    think: bool = True  # False: no extended thinking at all (fastest first words, for spoken replies)


# Set by Models.for_user for the turn being run; the Claude provider reads it.
claude_options: ContextVar[ClaudeOptions] = ContextVar("claude_options", default=ClaudeOptions())


def options_for(user: dict[str, Any]) -> ClaudeOptions:
    model = user.get("claude_model")
    effort = user.get("claude_effort")
    warm = user.get("claude_warm")
    return ClaudeOptions(
        model=model if model in CLAUDE_MODELS else None,
        effort=effort if effort in EFFORTS else None,
        warm=warm is None or bool(warm),
    )


class ClaudeWithBackup:
    """Claude, with the free model taking over while Claude doesn't answer (the
    Pro usage limit was hit, or the subscription ended). Claude is tried again
    after a pause that doubles each time it is still down, up to a few hours.

    The free model only takes over when Claude failed before running any tool:
    once a card has been graded, running the turn again would grade it twice."""

    FIRST_PAUSE = 30 * 60
    MAX_PAUSE = 6 * 3600

    def __init__(self, claude: LLMProvider, free: LLMProvider):
        self.claude = claude
        self.free = free
        self.down_until = 0.0
        self.pause = 0.0
        # Set when the free model took over; the bot tells the user once, then clears it.
        self.notice = False

    @property
    def is_down(self) -> bool:
        return time.monotonic() < self.down_until

    async def run(self, system: str, prompt: str, tools: list[ToolSpec], must_use_tool: bool = False) -> str:
        if self.is_down:
            return await self.free.run(system, prompt, tools, must_use_tool)
        used = []

        def watch(spec: ToolSpec) -> ToolSpec:
            async def handler(args: dict[str, Any]) -> str:
                used.append(spec.name)
                return await spec.handler(args)

            return ToolSpec(spec.name, spec.description, spec.schema, handler)

        try:
            reply = await self.claude.run(system, prompt, [watch(t) for t in tools], must_use_tool)
        except LLMError as e:
            if used:
                raise
            first = self.pause == 0
            self.pause = min(self.pause * 2, self.MAX_PAUSE) if self.pause else self.FIRST_PAUSE
            self.down_until = time.monotonic() + self.pause
            self.notice = self.notice or first
            log.warning("Claude unavailable, using the free model for %.0f min: %s", self.pause / 60, e)
            return await self.free.run(system, prompt, tools, must_use_tool)
        self.pause = 0.0
        return reply


class Models:
    """Picks the model for each user. Only owners may ever use Claude (it runs on
    the owner's own Pro subscription); guests always get the free model."""

    def __init__(self, owner_ids: frozenset[int], claude: LLMProvider, free: LLMProvider | None):
        self.owner_ids = owner_ids
        self.claude = claude
        self.free = free
        self.backup = ClaudeWithBackup(claude, free) if free is not None else None

    def pop_notice(self) -> bool:
        """Whether the free model just took over from Claude (reported once)."""
        if self.backup and self.backup.notice:
            self.backup.notice = False
            return True
        return False

    def name_for(self, user: dict[str, Any]) -> str:
        if user["id"] in self.owner_ids and user.get("llm") != FREE:
            return CLAUDE
        return FREE

    def for_user(self, user: dict[str, Any]) -> LLMProvider:
        if self.name_for(user) == CLAUDE:
            claude_options.set(options_for(user))
            return self.backup or self.claude
        if self.free is None:
            raise LLMError("the free model is not set up (no GEMINI_API_KEY, NVIDIA_API_KEY or FREE_LLM_API_KEY)")
        return self.free


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


def make_free_provider(
    api_key: str | None,
    base_url: str,
    model: str,
    fallbacks: tuple[str, ...] = (),
    gemini_key: str | None = None,
    gemini_models: tuple[str, ...] = (),
    nvidia_key: str | None = None,
    nvidia_models: tuple[str, ...] = (),
) -> LLMProvider | None:
    """The free model: Gemini first if it has a key, then NVIDIA, then Groq (or
    another OpenAI-style API). Each next one takes over when the one before is
    busy or out of its daily allowance."""
    from .openai_compat import OpenAICompatProvider, Slot

    slots: list[Slot] = []
    if gemini_key:
        slots += [Slot(GEMINI_BASE_URL, gemini_key, m) for m in gemini_models]
    if nvidia_key:
        slots += [Slot(NVIDIA_BASE_URL, nvidia_key, m) for m in nvidia_models]
    if api_key:
        slots += [Slot(base_url, api_key, m) for m in dict.fromkeys([model, *fallbacks])]
    return OpenAICompatProvider(slots=slots) if slots else None
