"""`agent/bridge.py`: our router as a callable an SDK can await, and the relay.

No SDK here at all. The bridge is the half of the SDK runtimes that has nothing to do
with a vendor, so it is tested without one — what a runner does with the raise is the
runner's test, in `test_sdk_anthropic_runtime.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from latent_intel import events as ev
from latent_intel.agent import bridge
from latent_intel.models import Effect, ToolSpec


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def spec(name: str = "wiki_get") -> ToolSpec:
    return ToolSpec(
        name=name,
        source_id="design",
        description=f"the {name} tool",
        effect=Effect.EXTERNAL_READ,
    )


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    return f"{source_id}/{name} says yes"


def test_a_drained_relay_gives_each_event_once() -> None:
    """The runtime drains once per round; a relay that kept what it handed over would
    replay the first round's tool calls under every later one."""
    relay = bridge.Relay()
    assert relay.drain() == []

    emitter = ev.Emitter(uuid4())
    first = emitter.emit(ev.ToolStarted, tool="a", source_id="s", arguments={})
    second = emitter.emit(ev.ToolStarted, tool="b", source_id="s", arguments={})
    relay.push(first)
    relay.push(second)

    assert relay.drain() == [first, second]
    assert relay.drain() == []


@pytest.mark.anyio
async def test_a_call_returns_the_output_and_leaves_the_pair_on_the_relay() -> None:
    """The SDK gets a string back; the events it knows nothing about wait for the
    runtime's generator."""
    relay = bridge.Relay()
    call = bridge.bridged(
        spec(), emitter=ev.Emitter(uuid4()), call_tool=_router, relay=relay
    )

    assert await call({"key": "gist"}) == "design/wiki_get says yes"

    started, result = relay.drain()
    assert isinstance(started, ev.ToolStarted)
    assert isinstance(result, ev.ToolResult)
    assert started.tool == "wiki_get" and started.source_id == "design"
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, never guessed
    assert result.ok and result.output == "design/wiki_get says yes"
    assert result.parent_id == started.event_id


@pytest.mark.anyio
async def test_a_failed_call_raises_with_the_connector_s_own_message() -> None:
    """A model told only that "the tool failed" calls it again the same way, so the
    text the connector produced is what the raise carries."""

    async def angry(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the store is unreachable")

    relay = bridge.Relay()
    call = bridge.bridged(
        spec(), emitter=ev.Emitter(uuid4()), call_tool=angry, relay=relay
    )

    with pytest.raises(bridge.BridgeError) as caught:
        await call({})
    assert str(caught.value) == "the store is unreachable"

    # The pair is relayed either way: a call that failed is a call that happened, and
    # a renderer that never saw it shows a turn with a silent gap in the middle.
    started, result = relay.drain()
    assert isinstance(started, ev.ToolStarted)
    assert isinstance(result, ev.ToolResult)
    assert not result.ok and result.error == "the store is unreachable"


@pytest.mark.anyio
async def test_a_session_that_handed_over_no_router_fails_the_call_not_the_turn() -> (
    None
):
    """`turn.dispatch` never raises, so this is the only place the absence shows up —
    as a failed call the model is told about."""
    relay = bridge.Relay()
    call = bridge.bridged(
        spec(), emitter=ev.Emitter(uuid4()), call_tool=None, relay=relay
    )
    with pytest.raises(bridge.BridgeError) as caught:
        await call({})
    assert "no tool router" in str(caught.value)
