"""Our tool router, as a callable an SDK's own agent loop can invoke.

A runtime that owns its loop calls `turn.dispatch` and yields the pair of events it
returns. A runtime that hands the loop to an SDK cannot: the runner executes tools
inside its own `__anext__`, after our loop body has returned and before the next
request opens, so there is no `yield` in scope at the moment the tool runs. This module
is the seam that survives that — a plain callable the SDK can await, and a place to put
the events it produced until our generator is running again.

**The relay is a list, deliberately not a task group.** An async generator holding an
anyio task group open across a `yield` is the cancel-scope hazard `connect_many` met:
the scope is entered in one task's context and exited in another's. A list appended to
by the SDK's task and drained by ours needs none of that, and the events are already
stamped, so nothing about their order depends on when they are read.

**Buffered, not live.** `ToolStarted` therefore renders when the tool *finishes* and
the next request opens, not when it starts. Ordering is still correct — `Emitter`
stamps `sequence` at emit time, and the relay is drained before the next round's tokens
are yielded — so the cost is latency in the display, not a scrambled stream. Sources
answer in milliseconds today. The trigger for making it live, the same one ADR-001 set
for parallel dispatch, is a slow tool showing up in a trace: at that point the relay
becomes a memory channel the runtime selects on, and nothing outside this file moves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .. import events as ev
from ..models import ToolSpec
from . import turn


class BridgeError(Exception):
    """A tool call that failed, carrying the text the model should see.

    Raised rather than returned because every SDK runner reads a raised exception as
    the tool's error channel, and each maps it to its own error shape — a `ToolError`
    here, a returned string there. The message is the connector's own, because a model
    told only that "the tool failed" will call it again the same way.
    """


class Relay:
    """Events emitted while an SDK runs our tool, held until the runtime's generator
    can yield them.

    Not thread-safe and does not need to be: an SDK runner awaits tools inside the same
    event loop as the generator draining this.
    """

    def __init__(self) -> None:
        self._events: list[ev.AgentEvent] = []

    def push(self, *events: ev.AgentEvent) -> None:
        """Hold events until the next drain."""
        self._events.extend(events)

    def drain(self) -> list[ev.AgentEvent]:
        """Everything held since the last call, in order, and clear."""
        held, self._events = self._events, []
        return held


async def run(
    spec: ToolSpec,
    *,
    emitter: ev.Emitter,
    relay: Relay,
    call_tool: turn.ToolRouter | None,
    arguments: dict[str, Any],
) -> str:
    """One tool call, routed through `turn.dispatch` and relayed.

    The router is an argument rather than something closed over, because one SDK
    decides it per call: an OpenAI-protocol call whose arguments are not valid JSON is
    dispatched through a router that raises, so the model is shown its own mistake
    rather than having the fragment passed to a connector. A runner whose tool objects
    are built once, before any arguments exist, cannot bind that router at build time.
    """
    started, result = await turn.dispatch(
        call_tool,
        spec,
        source_id=spec.source_id,
        name=spec.name,
        arguments=arguments,
        emitter=emitter,
    )
    relay.push(started, result)
    if not result.ok:
        raise BridgeError(result.error)
    return result.output


def bridged(
    spec: ToolSpec,
    *,
    emitter: ev.Emitter,
    call_tool: turn.ToolRouter | None,
    relay: Relay,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """One wired tool, as the async callable an SDK runner awaits.

    The body is `run`, so a tool reached through a runner is routed, reported and timed
    exactly as one reached through our own loop — the point of offloading the loop is
    that execution stays ours. For a runner that hands over the arguments already
    decoded and always through the one router, this is the whole seam; the other kind
    calls `run` itself.
    """

    async def call(arguments: dict[str, Any]) -> str:
        return await run(
            spec,
            emitter=emitter,
            relay=relay,
            call_tool=call_tool,
            arguments=arguments,
        )

    return call


__all__ = ["BridgeError", "Relay", "bridged", "run"]
