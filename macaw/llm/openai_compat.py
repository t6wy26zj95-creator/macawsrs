"""Any model behind an OpenAI-style chat completions API, with tool calling.

Used for the free model (Groq by default). Groq's free tier allows few tokens
per minute, so a 429 is waited out a couple of times before giving up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from . import LLMError, ToolSpec

log = logging.getLogger(__name__)

MAX_RATE_WAIT = 30  # seconds we're willing to wait on one 429
RATE_RETRIES = 2


class OpenAICompatProvider:
    def __init__(self, base_url: str, api_key: str, model: str, max_turns: int = 10, timeout: float = 90):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.max_turns = max_turns
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _post(self, session: aiohttp.ClientSession, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(RATE_RETRIES + 1):
            try:
                async with session.post(self.url, json=body, headers=headers) as r:
                    if r.status == 429 and attempt < RATE_RETRIES:
                        wait = _retry_after(r.headers.get("retry-after"))
                        if wait <= MAX_RATE_WAIT:
                            log.info("free model rate limited, waiting %.0fs", wait)
                            await asyncio.sleep(wait)
                            continue
                    if r.status != 200:
                        text = (await r.text())[:300]
                        raise LLMError(f"HTTP {r.status}: {text}")
                    return await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise LLMError(str(e) or type(e).__name__) from e
        raise LLMError("rate limited")

    async def run(self, system: str, prompt: str, tools: list[ToolSpec]) -> str:
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
            for _ in range(self.max_turns):
                body: dict[str, Any] = {"model": self.model, "messages": messages}
                if tool_defs:
                    body["tools"] = tool_defs
                data = await self._post(session, body)
                try:
                    msg = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError) as e:
                    raise LLMError(f"unexpected response: {str(data)[:300]}") from e
                if usage := data.get("usage"):
                    log.info(
                        "free model call: %s input / %s output tokens",
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


def _retry_after(raw: str | None) -> float:
    try:
        return max(1.0, float(raw)) if raw else 10.0
    except ValueError:
        return 10.0
