"""Models behind OpenAI-style chat completions APIs, with tool calling.

Used for the free model: Gemini, NVIDIA and/or Groq, tried in order. Free tiers
have small limits (Gemini Flash: 20 requests a day; Groq: about 8K tokens a
minute, and one bot call is around 6K), counted per model. So on a 429 we move
to the next model, and when all of them are limited for a short while we wait
for the soonest one to free up. A model whose daily limit is used up is skipped
until the limit resets, so later turns don't keep asking it first. A model that
errors in another way is skipped for a minute, or for a day if it doesn't exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import aiohttp

from . import LLMError, ToolSpec

log = logging.getLogger(__name__)

MAX_RATE_WAIT = 65  # seconds we're willing to wait for one rate limit to clear
RATE_WAITS = 2  # how many such waits one turn may take
BROKEN_PAUSE = 60  # seconds a model that errored is skipped
MISSING_PAUSE = 24 * 3600  # a model the API doesn't have (404) or won't let this key use (403)
AUTH_PAUSE = 3600  # the key itself was refused (401)
DAILY_FALLBACK = 3600  # daily limit hit but no reset time known: look again in an hour


@dataclass(frozen=True)
class Slot:
    """One model at one API."""

    base_url: str
    api_key: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.base_url}|{self.model}"

    @property
    def is_gemini(self) -> bool:
        return "generativelanguage.googleapis.com" in self.base_url


class RateLimited(Exception):
    def __init__(self, wait: float, daily: bool = False):
        self.wait = wait
        self.daily = daily


class HTTPError(LLMError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str | list[str] = (),
        max_turns: int = 10,
        timeout: float = 90,
        slots: list[Slot] | None = None,
    ):
        models = [model] if isinstance(model, str) else list(model)
        self.slots = list(slots) if slots else [Slot(base_url, api_key, m) for m in models]
        self.max_turns = max_turns
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        # When each slot can be tried again (monotonic seconds), shared across turns.
        self.free_at: dict[str, float] = {}

    @property
    def model(self) -> str:
        return self.slots[0].model

    async def _post(self, session: aiohttp.ClientSession, slot: Slot, body: dict[str, Any]) -> dict[str, Any]:
        url = slot.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {slot.api_key}"}
        try:
            async with session.post(url, json=body, headers=headers) as r:
                if r.status == 429:
                    text = (await r.text())[:2000]
                    wait = _retry_after(r.headers.get("retry-after"))
                    if _is_daily(text):
                        raise RateLimited(max(wait, _daily_wait(slot)), daily=True)
                    raise RateLimited(wait)
                if r.status != 200:
                    text = (await r.text())[:300]
                    raise HTTPError(r.status, f"{slot.model}: HTTP {r.status}: {text}")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LLMError(f"{slot.model}: {e or type(e).__name__}") from e

    async def _try(self, session: aiohttp.ClientSession, slot: Slot, body: dict[str, Any]) -> dict[str, Any]:
        full = _body_for(slot, body)
        try:
            return await self._post(session, slot, full)
        except HTTPError as e:
            optional = [k for k in ("tool_choice", "reasoning_effort") if k in full]
            if e.status == 400 and optional:
                # Not every API accepts tool_choice="required" or reasoning_effort; ask again without them.
                log.info("%s rejected the request, retrying without %s", slot.model, ", ".join(optional))
                return await self._post(session, slot, {k: v for k, v in full.items() if k not in optional})
            raise

    async def _complete(self, session: aiohttp.ClientSession, body: dict[str, Any]) -> dict[str, Any]:
        """One completion on the first model that is available."""
        waits = 0
        error: LLMError | None = None
        while True:
            now = time.monotonic()
            for slot in self.slots:
                if self.free_at.get(slot.key, 0) > now:
                    continue
                try:
                    return await self._try(session, slot, body)
                except RateLimited as e:
                    self.free_at[slot.key] = time.monotonic() + e.wait
                    what = "daily limit used up" if e.daily else "rate limited"
                    log.info("free model %s %s, skipped for %.0fs", slot.model, what, e.wait)
                except LLMError as e:
                    pause = _pause_for(e)
                    self.free_at[slot.key] = time.monotonic() + pause
                    log.warning("free model failed, skipped for %.0fs: %s", pause, e)
                    error = e
            wait = min(self.free_at.get(s.key, 0) for s in self.slots) - time.monotonic()
            if waits >= RATE_WAITS or wait > MAX_RATE_WAIT:
                raise error or LLMError("rate limited on every free model")
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


def _body_for(slot: Slot, body: dict[str, Any]) -> dict[str, Any]:
    out = {"model": slot.model, **body}
    if slot.model.startswith("openai/gpt-oss") or slot.is_gemini:
        # Short replies don't need long hidden reasoning, and it all counts toward the limit.
        out["reasoning_effort"] = "low"
    if slot.is_gemini and "tools" in out:
        # Gemini's function schemas are a subset of JSON Schema without additionalProperties.
        out["tools"] = [
            {**t, "function": {**t["function"], "parameters": _strip_extra(t["function"]["parameters"])}}
            for t in out["tools"]
        ]
    return out


def _strip_extra(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_extra(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_extra(v) for v in schema]
    return schema


def _pause_for(e: LLMError) -> float:
    status = getattr(e, "status", None)
    if status in (403, 404):
        return MISSING_PAUSE
    if status == 401:
        return AUTH_PAUSE
    return BROKEN_PAUSE


def _is_daily(body: str) -> bool:
    """Whether a 429 is about a daily allowance rather than a per-minute one.
    Gemini names the quota (GenerateRequestsPerDayPerProjectPerModel), Groq
    says "requests per day (RPD)" or "tokens per day (TPD)"."""
    low = body.lower()
    return "perday" in low or "per day" in low or "(rpd)" in low or "(tpd)" in low


def _daily_wait(slot: Slot, now: datetime | None = None) -> float:
    if slot.is_gemini:
        # Gemini's free allowance resets at midnight Pacific time.
        pacific = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo("America/Los_Angeles"))
        reset = (pacific + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        return (reset - pacific).total_seconds()
    return DAILY_FALLBACK


def _retry_after(raw: str | None) -> float:
    try:
        return max(1.0, float(raw)) if raw else 10.0
    except ValueError:
        return 10.0
