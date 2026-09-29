"""A scripted `claude_agent_sdk.query`, yielding the SDK's own message types.

**`query` is faked, never the binary.** `ClaudeAgentSdkRuntime.query_factory` returns
the callable used in place of `claude_agent_sdk.query`, so this stands in for that
function and nothing below it: no process, no model, no network. What must stay under
test is
that **the binary calls our tools** — between its assistant message asking for one and
the user message carrying the result — so a call to one of ours is made the way the SDK
makes it for the binary: a JSON-RPC `tools/call` through the SDK's own `SdkMcpBridge`,
into the in-process server the runtime built. Schema validation and the not-found reply
are therefore the SDK's, not an imitation of them.

The messages are the SDK's real dataclasses, because the runtime matches them with
`isinstance`; a field the SDK renames breaks here first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import claude_agent_sdk as sdk
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from latent_intel.agent.runtimes.claude_agent_sdk import SERVER


@dataclass
class Text:
    """One text delta, from the main agent unless `parent` names a subagent's call."""

    text: str
    parent: str | None = None


@dataclass
class Call:
    """One tool call: the assistant message asking for it, then the user message with
    its result. A call to one of our tools is served by the runtime's own server; any
    other name — a built-in — answers with `output`."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = "toolu_1"
    parent: str | None = None
    output: str = ""
    is_error: bool = False
    #: What reaches the stream: `"both"` messages, `"asked"` only (a tool started that
    #: never answers), or `"none"` (our tool runs, and the binary dies before saying
    #: so — the case where only the relay knows it happened).
    shown: str = "both"


@dataclass
class Result:
    """The closing `ResultMessage`. Success by default."""

    result: str = ""
    subtype: str = "success"
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "session-1"
    total_cost_usd: float | None = 0.0123
    usage: dict[str, Any] | None = field(
        default_factory=lambda: {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "server_tool_use": {"web_search_requests": 0},
        }
    )
    api_error_status: int | None = None
    errors: list[str] | None = None


@dataclass
class Raise:
    """An exception out of the iterator, where the SDK would raise one."""

    error: BaseException


Step = Text | Call | Result | Raise


class FakeQuery:
    """`query(prompt=..., options=...)`, scripted. Records what it was handed, and
    whether it was closed — which the runtime has to do in its own task, promptly,
    however the turn ends."""

    def __init__(self, *steps: Step) -> None:
        self.steps = steps
        self.prompt: Any = None
        self.options: Any = None
        self.calls = 0
        self.closed = False
        #: The JSON-RPC results our server returned, by tool-use id.
        self.served: dict[str, dict[str, Any]] = {}
        self._bridge: SdkMcpBridge | None = None

    def query(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        """What the runtime calls in place of `claude_agent_sdk.query`."""
        self.calls += 1
        self.prompt = prompt
        self.options = options
        return self._run()

    async def _run(self) -> AsyncIterator[Any]:
        try:
            async for message in self._outputs():
                yield message
        finally:
            self.closed = True
            if self._bridge is not None:
                await self._bridge.aclose()

    async def _outputs(self) -> AsyncIterator[Any]:
        async for message in self._messages():
            if isinstance(message, Call):
                asked, answered = await self._serve(message)
                if message.shown in ("both", "asked"):
                    yield asked
                if message.shown == "both":
                    yield answered
            else:
                yield message

    async def _messages(self) -> AsyncIterator[Any]:
        yield sdk.SystemMessage(subtype="init", data={"session_id": "session-1"})
        for step in self.steps:
            if isinstance(step, Text):
                yield sdk.StreamEvent(
                    uuid="u",
                    session_id="session-1",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": step.text},
                    },
                    parent_tool_use_id=step.parent,
                )
            elif isinstance(step, Call):
                yield step
            elif isinstance(step, Result):
                yield sdk.ResultMessage(
                    subtype=step.subtype,
                    duration_ms=42,
                    duration_api_ms=40,
                    is_error=step.is_error,
                    num_turns=step.num_turns,
                    session_id=step.session_id,
                    total_cost_usd=step.total_cost_usd,
                    usage=step.usage,
                    result=step.result,
                    api_error_status=step.api_error_status,
                    errors=step.errors,
                )
            else:
                raise step.error

    async def _connect(self) -> SdkMcpBridge:
        """The SDK's bridge onto the runtime's server, after the handshake the binary
        performs first. Opened once, on the first call to one of our tools."""
        if self._bridge is not None:
            return self._bridge
        servers = self.options.mcp_servers or {}
        if SERVER not in servers:
            raise AssertionError("the runtime served no tools, but one was called")
        bridge = SdkMcpBridge(SERVER, servers[SERVER]["instance"])
        await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "fake-claude", "version": "0"},
                },
            }
        )
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._bridge = bridge
        return bridge

    async def _serve(self, call: Call) -> list[Any]:
        """The assistant message asking for `call`, then its result."""
        asked = sdk.AssistantMessage(
            content=[
                sdk.ToolUseBlock(id=call.id, name=call.name, input=call.arguments)
            ],
            model="claude-fake",
            parent_tool_use_id=call.parent,
        )
        prefix = f"mcp__{SERVER}__"
        if call.name.startswith(prefix):
            bridge = await self._connect()
            response = await bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": len(self.served) + 1,
                    "method": "tools/call",
                    "params": {
                        "name": call.name[len(prefix) :],
                        "arguments": call.arguments,
                    },
                }
            )
            outcome = (response or {}).get("result") or {}
            self.served[call.id] = outcome
            text = "\n".join(
                str(block.get("text", "")) for block in outcome.get("content") or []
            )
            failed = bool(outcome.get("isError"))
        else:
            text, failed = call.output, call.is_error
        answered = sdk.UserMessage(
            content=[
                sdk.ToolResultBlock(tool_use_id=call.id, content=text, is_error=failed)
            ],
            parent_tool_use_id=call.parent,
        )
        return [asked, answered]
