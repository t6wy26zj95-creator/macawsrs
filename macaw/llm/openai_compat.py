"""Any model behind an OpenAI-style chat completions API, with tool calling.

Used for the free model (Groq by default). Groq's free tier allows only about
8K tokens per minute per model, and one bot call is around 4K, so a grading
turn often runs into the limit. The limit is counted per model, so on a 429 we
move to the next model in the list, and when all of them are limited we wait
for the soonest one to free up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from . import LLMError, ToolSpec

log = logging.getLogger(__name__)

MAX_RATE_WAIT = 65  # seconds we're willing to wait for one rate limit to clear
RATE_WAITS = 2  # how many such waits one turn may take


class RateLimited(Exception):
    def __init__(self, wait: float):
        self.wait = wait


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str | list[str],
        max_turns: int = 10,
        timeout: float = 90,
    ):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.models = [model] if isinstance(model, str) else list(model)
        self.max_turns = max_turns
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        # When each model's rate limit clears (monotonic seconds), shared across turns.
        self.free_at: dict[str, float] = {}

    @property
    def model(self) -> str:
        return self.models[0]

    async def _post(self, session: aiohttp.ClientSession, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with session.post(self.url, json=body, headers=headers) as r:
                if r.status == 429:
                    raise RateLimited(_retry_after(r.headers.get("retry-after")))
                if r.status != 200:
                    text = (await r.text())[:300]
                    raise LLMError(f"HTTP {r.status}: {text}")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LLMError(str(e) or type(e).__name__) from e

    async def _complete(self, session: aiohttp.ClientSession, body: dict[str, Any]) -> dict[str, Any]:
        """One completion on the first model that isn't rate limited."""
        waits = 0
        while True:
            now = time.monotonic()
            for model in self.models:
                if self.free_at.get(model, 0) > now:
                    continue
                try:
                    return await self._post(session, _body_for(model, body))
                except RateLimited as e:
                    self.free_at[model] = time.monotonic() + e.wait
                    log.info("free model %s rate limited for %.0fs", model, e.wait)
            wait = min(self.free_at.get(m, 0) for m in self.models) - time.monotonic()
            if waits >= RATE_WAITS or wait > MAX_RATE_WAIT:
                raise LLMError("rate limited on every free model")
            waits += 1
            await asyncio.sleep(max(wait, 0.5))

    async def run(self, system: str, prompt: str, tools: list[ToolSpec], must_use_tool: bool = False) -> str:
        by_name = {t.name: t for t in tools}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        tool_defs = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
            for t in tools
        ]
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            for round_no in range(self.max_turns):
                body: dict[str, Any] = {"messages": messages}
                if tool_defs:
                    body["tools"] = tool_defs
                    if must_use_tool and round_no == 0:
                        # Smaller models often reply "you got it" without calling grade_card.
                        body["tool_choice"] = "required"
                data = await self._complete(session, body)
                try:
                    msg = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError) as e:
                    raise LLMError(f"unexpected response: {str(data)[:300]}") from e
                if usage := data.get("usage"):
                    log.info(
                        "free model call (%s): %s input / %s output tokens",
                        data.get("model"),
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                    )
                calls = msg.get("tool_calls") or []
                if not calls:
                    return (msg.get("content") or "").strip()
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for call in calls:
                    fn = call.get("function") or {}
                    messages.append(
                        {"role": "tool", "tool_call_id": call.get("id"), "content": await _call(by_name, fn)}
                    )
        raise LLMError("too many tool rounds")


async def _call(by_name: dict[str, ToolSpec], fn: dict[str, Any]) -> str:
    spec = by_name.get(fn.get("name", ""))
    if spec is None:
        return f"Error: unknown tool {fn.get('name')!r}"
    try:
        args = json.loads(fn.get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        return await spec.handler(args)
    except Exception as e:  # tool errors go back to the model, not to the user
        log.exception("tool %s failed", spec.name)
        return f"Error: {e}"


def _body_for(model: str, body: dict[str, Any]) -> dict[str, Any]:
    out = {"model": model, **body}
    if model.startswith("openai/gpt-oss"):
        # Short replies don't need long hidden reasoning, and it all counts toward the limit.
        out["reasoning_effort"] = "low"
    return out


def _retry_after(raw: str | None) -> float:
    try:
        return max(1.0, float(raw)) if raw else 10.0
    except ValueError:
        return 10.0
