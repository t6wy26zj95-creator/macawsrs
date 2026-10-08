"""Claude through the Claude Agent SDK, which drives the Claude Code CLI.

Authentication uses the Pro subscription: set CLAUDE_CODE_OAUTH_TOKEN to the
token printed by `claude setup-token`. No API key is involved.
"""

from __future__ import annotations

import logging
import os
import tempfile

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from . import LLMError, ToolSpec

log = logging.getLogger(__name__)

SERVER = "srs"


class ClaudeCodeProvider:
    def __init__(self, model: str | None = None, max_turns: int = 10):
        self.model = model
        self.max_turns = max_turns
        # Claude Code runs in an empty scratch directory so it never sees the bot's files.
        self.workdir = tempfile.mkdtemp(prefix="macaw-claude-")

    def _sdk_tools(self, tools: list[ToolSpec]):
        out = []
        for spec in tools:

            def make(spec: ToolSpec):
                @tool(spec.name, spec.description, spec.schema)
                async def handler(args):
                    try:
                        text = await spec.handler(args)
                    except Exception as e:  # tool errors go back to Claude, not to the user
                        log.exception("tool %s failed", spec.name)
                        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
                    return {"content": [{"type": "text", "text": text}]}

                return handler

            out.append(make(spec))
        return out

    async def run(self, system: str, prompt: str, tools: list[ToolSpec], must_use_tool: bool = False) -> str:
        # Claude grades reliably from the system prompt, so must_use_tool isn't needed.
        server = create_sdk_mcp_server(name=SERVER, tools=self._sdk_tools(tools))
        options = ClaudeAgentOptions(
            system_prompt=system,
            tools=[],  # no built-in Claude Code tools (no shell, no files)
            mcp_servers={SERVER: server},
            strict_mcp_config=True,
            allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in tools],
            setting_sources=[],
            max_turns=self.max_turns,
            model=self.model,
            cwd=self.workdir,
            env={
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            extra_args={"no-session-persistence": None},
        )
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            log.warning("CLAUDE_CODE_OAUTH_TOKEN is not set; Claude Code will use any stored login")

        texts: list[str] = []
        result: ResultMessage | None = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        turn_text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                        if turn_text.strip():
                            texts.append(turn_text)
                    elif isinstance(msg, ResultMessage):
                        result = msg
        except Exception as e:
            raise LLMError(str(e)) from e

        if result is not None and result.usage:
            u = result.usage
            log.info(
                "claude turn: %s input (+%s cached) / %s output tokens, %s tool rounds",
                u.get("input_tokens"),
                (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                u.get("output_tokens"),
                result.num_turns,
            )
        if result is not None and result.is_error:
            raise LLMError(f"{result.subtype}: {result.errors or result.api_error_status}")
        # The final reply is the text after the last tool round.
        if result is not None and result.result:
            return result.result.strip()
        return texts[-1].strip() if texts else ""
