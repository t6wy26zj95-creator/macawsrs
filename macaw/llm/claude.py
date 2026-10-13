"""Claude through the Claude Agent SDK, which drives the Claude Code CLI.

Authentication uses the Pro subscription: set CLAUDE_CODE_OAUTH_TOKEN to the
token printed by `claude setup-token`. No API key is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from . import ClaudeOptions, LLMError, ToolSpec, claude_options

log = logging.getLogger(__name__)

SERVER = "srs"

# Claude Code sometimes reports a lost login or a used-up plan as an ordinary
# reply instead of an error. Those must not reach the user as the bot's words.
CLI_ERROR = re.compile(
    r"^(claude ai usage limit reached|you've hit your (usage )?limit|you've reached your usage limit|"
    r"invalid api key|please run /login|oauth token (has )?expired|credit balance is too low|api error: \d)",
    re.IGNORECASE,
)


class _Ready:
    """A Claude Code process that is started and waiting for its next turn.

    Its tools are fixed when it starts, so each tool looks up this turn's
    handler in `handlers` when it is called."""

    def __init__(self, handlers: dict[str, ToolSpec]):
        self.client: ClaudeSDKClient | None = None
        self.handlers = handlers
        self.ran = False  # a tool ran in the current turn
        self.born = time.monotonic()


class ClaudeCodeProvider:
    # Each running Claude Code process takes a little memory on the server,
    # so only the last few kinds of turns are kept ready.
    MAX_READY = 2
    # A process left waiting this long is replaced by a fresh one before use.
    MAX_READY_AGE = 3600

    def __init__(self, model: str | None = None, max_turns: int = 10):
        self.model = model
        self.max_turns = max_turns
        # Claude Code runs in an empty scratch directory so it never sees the bot's files.
        self.workdir = tempfile.mkdtemp(prefix="macaw-claude-")
        self._ready: dict[tuple, _Ready] = {}
        self._warming: set[tuple] = set()
        self._tasks: set[asyncio.Task] = set()

    def _sdk_tools(self, tools: list[ToolSpec], ready: _Ready):
        out = []
        for spec in tools:

            def make(spec: ToolSpec):
                @tool(spec.name, spec.description, spec.schema)
                async def handler(args):
                    ready.ran = True
                    try:
                        text = await ready.handlers[spec.name].handler(args)
                    except Exception as e:  # tool errors go back to Claude, not to the user
                        log.exception("tool %s failed", spec.name)
                        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
                    return {"content": [{"type": "text", "text": text}]}

                return handler

            out.append(make(spec))
        return out

    def _model_for(self, opts: ClaudeOptions) -> str | None:
        if opts.model is None:
            return self.model
        if self.model and opts.model in self.model:  # e.g. "opus" and CLAUDE_MODEL=claude-opus-5-5
            return self.model
        return opts.model

    def _key(self, system: str, tools: list[ToolSpec], opts: ClaudeOptions) -> tuple:
        sig = json.dumps([[t.name, t.description, t.schema] for t in tools], sort_keys=True)
        return (system, sig, self._model_for(opts), opts.effort)

    async def _start(self, system: str, tools: list[ToolSpec], opts: ClaudeOptions) -> _Ready:
        ready = _Ready({t.name: t for t in tools})
        server = create_sdk_mcp_server(name=SERVER, tools=self._sdk_tools(tools, ready))
        model = self._model_for(opts)
        options = ClaudeAgentOptions(
            system_prompt=system,
            tools=[],  # no built-in Claude Code tools (no shell, no files)
            mcp_servers={SERVER: server},
            strict_mcp_config=True,
            allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in tools],
            setting_sources=[],
            max_turns=self.max_turns,
            model=model,
            # Haiku doesn't take an effort level.
            effort=None if model and "haiku" in model else opts.effort,
            cwd=self.workdir,
            env={
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            extra_args={"no-session-persistence": None},
        )
        ready.client = ClaudeSDKClient(options=options)
        await ready.client.connect()
        return ready

    async def _close(self, ready: _Ready) -> None:
        if ready.client is not None:
            with contextlib.suppress(Exception):
                await ready.client.disconnect()

    async def _warm(self, key: tuple, system: str, tools: list[ToolSpec], opts: ClaudeOptions) -> None:
        """Start the next process for this kind of turn now, so the next message doesn't wait for it."""
        if key in self._warming:
            return
        self._warming.add(key)
        try:
            ready = await self._start(system, tools, opts)
        except Exception as e:
            log.warning("couldn't start a waiting Claude Code process: %s", e)
            return
        finally:
            self._warming.discard(key)
        old = self._ready.pop(key, None)
        self._ready[key] = ready
        if old:
            await self._close(old)
        while len(self._ready) > self.MAX_READY:
            await self._close(self._ready.pop(next(iter(self._ready))))

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _close_all(self) -> None:
        while self._ready:
            await self._close(self._ready.pop(next(iter(self._ready))))

    async def run(self, system: str, prompt: str, tools: list[ToolSpec], must_use_tool: bool = False) -> str:
        # Claude grades reliably from the system prompt, so must_use_tool isn't needed.
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            log.warning("CLAUDE_CODE_OAUTH_TOKEN is not set; Claude Code will use any stored login")
        opts = claude_options.get()
        if not opts.warm and self._ready:
            await self._close_all()
        key = self._key(system, tools, opts)
        t0 = time.monotonic()
        ready = self._ready.pop(key, None)
        if ready and time.monotonic() - ready.born > self.MAX_READY_AGE:
            await self._close(ready)
            ready = None
        was_waiting = ready is not None
        try:
            try:
                if ready is None:
                    ready = await self._start(system, tools, opts)
                started = time.monotonic() - t0
                ready.handlers.update({t.name: t for t in tools})
                texts, result = await self._turn(ready.client, prompt)
            except Exception as e:
                # The waiting process may have died; start a fresh one, unless a tool already ran.
                if not was_waiting or ready is None or ready.ran:
                    raise
                log.warning("waiting Claude Code process failed (%s); starting a fresh one", e)
                await self._close(ready)
                was_waiting = False
                ready = await self._start(system, tools, opts)
                started = time.monotonic() - t0
                texts, result = await self._turn(ready.client, prompt)
            elapsed = time.monotonic() - t0
        except Exception as e:
            raise LLMError(str(e)) from e
        finally:
            # Closing the used process and starting the next one happen after the reply is sent.
            if ready is not None:
                self._background(self._close(ready))
            if opts.warm:
                self._background(self._warm(key, system, tools, opts))

        if result is not None and result.usage:
            u = result.usage
            log.info(
                "claude turn: %.1fs (%.1fs to start Claude Code%s), model %s, effort %s, "
                "%s input (+%s cached) / %s output tokens, %s tool rounds",
                elapsed,
                started,
                ", was ready" if was_waiting else "",
                self._model_for(opts) or "default",
                opts.effort or "default",
                u.get("input_tokens"),
                (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                u.get("output_tokens"),
                result.num_turns,
            )
        if result is not None and result.is_error:
            raise LLMError(f"{result.subtype}: {result.errors or result.api_error_status}")
        # The final reply is the text after the last tool round.
        if result is not None and result.result:
            reply = result.result.strip()
        else:
            reply = texts[-1].strip() if texts else ""
        if len(reply) < 200 and CLI_ERROR.match(reply):
            raise LLMError(reply)
        return reply

    @staticmethod
    async def _turn(client: ClaudeSDKClient, prompt: str) -> tuple[list[str], ResultMessage | None]:
        texts: list[str] = []
        result: ResultMessage | None = None
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                turn_text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                if turn_text.strip():
                    texts.append(turn_text)
            elif isinstance(msg, ResultMessage):
                result = msg
        return texts, result
