"""The MCP connector, against a real server running in-process.

No subprocess, no network, no fixture files. `Client` accepts a server object directly,
so these exercise the actual protocol — initialize, list, call, read — rather than a
mock of it. The one thing they do not cover is transport selection, which is the SDK's
job and tested there.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import types
from mcp.server import MCPServer

from latent_intel.connectors import base as connectors
from latent_intel.connectors.mcp import McpConnector, _effect_of, _transport
from latent_intel.models import Capability, ConnectError, Effect
from latent_intel.session import Session

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_server(*, with_resources: bool = True) -> MCPServer:
    """A server with one honestly-annotated tool, one destructive one, and one that
    declares nothing — the three cases the effect mapping has to get right."""
    server = MCPServer("probe", instructions="A probe server.")

    @server.tool(
        annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
    )
    def echo(text: str) -> str:
        """Echo it back."""
        return f"echo: {text}"

    @server.tool(annotations=types.ToolAnnotations(destructive_hint=True))
    def wipe(target: str) -> str:
        """Delete something."""
        return f"wiped {target}"

    @server.tool()
    def mystery(x: str) -> str:
        """Declares no annotations at all."""
        return "?"

    if with_resources:

        @server.resource("note://alpha")
        def alpha() -> str:
            """Notes about retrieval."""
            return "Retrieval is how a system finds material before generating."

        @server.resource("note://beta")
        def beta() -> str:
            """Notes about compaction."""
            return "Rewriting an accumulated context destroys it."

    return server


async def connect(server: Any, source_id: str = "probe") -> McpConnector:
    connector = McpConnector(source_id, server)
    await connector.aopen()
    return connector


# -- capabilities -----------------------------------------------------------


async def test_capabilities_come_from_the_server_not_from_tool_names() -> None:
    connector = await connect(build_server())
    descriptor = connector.describe()
    assert set(descriptor.capabilities) == {
        Capability.TOOLS,
        Capability.SEARCH,
        Capability.FETCH,
    }
    assert descriptor.detail["tools"] == 3
    assert descriptor.detail["resources"] == 2
    await connector.aclose()


async def test_a_server_with_no_resources_does_not_claim_search() -> None:
    """Declaring the capability is not the same as having any. `lw serve` advertises
    `resources` and registers none; claiming SEARCH there would put an always-empty
    heading in every result set."""
    connector = await connect(build_server(with_resources=False))
    assert connector.describe().capabilities == [Capability.TOOLS]
    await connector.aclose()


async def test_the_declared_capabilities_are_actually_implemented() -> None:
    """The same check `open_connector` runs — and it has to run *after* aopen, because
    an MCP server's capabilities are unknown until it has been started and asked."""
    connector = await connect(build_server())
    connectors.verify_capabilities(connector)  # type: ignore[arg-type]
    await connector.aclose()


# -- effects ----------------------------------------------------------------


async def test_effects_are_read_from_annotations() -> None:
    connector = await connect(build_server())
    by_name = {t.name: t for t in connector.tools()}

    # read_only + closed world: touches nothing outside the server.
    assert by_name["echo"].effect is Effect.NONE
    assert by_name["echo"].effect_declared is True

    assert by_name["wipe"].effect is Effect.DESTRUCTIVE
    assert by_name["wipe"].effect_declared is True
    await connector.aclose()


async def test_an_unannotated_tool_is_assumed_to_write_and_says_so() -> None:
    """Absence is not permission. The safe end of the guess, recorded as a guess so
    `intel doctor` can list it rather than treating it as a fact."""
    connector = await connect(build_server())
    mystery = next(t for t in connector.tools() if t.name == "mystery")
    assert mystery.effect is Effect.EXTERNAL_WRITE
    assert mystery.effect_declared is False
    assert mystery.effect.writes
    assert connector.describe().detail["undeclared_effects"] == 1
    await connector.aclose()


@pytest.mark.parametrize(
    ("annotations", "expected", "declared"),
    [
        (None, Effect.EXTERNAL_WRITE, False),
        (types.ToolAnnotations(), Effect.EXTERNAL_WRITE, False),
        (types.ToolAnnotations(destructive_hint=True), Effect.DESTRUCTIVE, True),
        (types.ToolAnnotations(read_only_hint=True), Effect.EXTERNAL_READ, True),
        (
            types.ToolAnnotations(read_only_hint=True, open_world_hint=False),
            Effect.NONE,
            True,
        ),
        (types.ToolAnnotations(read_only_hint=False), Effect.EXTERNAL_WRITE, True),
        # destructive wins over read_only when a server contradicts itself.
        (
            types.ToolAnnotations(read_only_hint=True, destructive_hint=True),
            Effect.DESTRUCTIVE,
            True,
        ),
    ],
)
def test_effect_mapping(annotations: Any, expected: Effect, declared: bool) -> None:
    assert _effect_of(annotations) == (expected, declared)


# -- tools and resources ----------------------------------------------------


async def test_tools_are_namespaced_by_source() -> None:
    """Two servers may both offer `search` without one shadowing the other."""
    connector = await connect(build_server(), source_id="gh")
    assert "gh.echo" in {t.qualified for t in connector.tools()}
    await connector.aclose()


async def test_calling_a_tool_returns_its_text() -> None:
    connector = await connect(build_server())
    assert await connector.call("echo", {"text": "hi"}) == "echo: hi"
    await connector.aclose()


async def test_a_failing_call_returns_the_error_rather_than_raising() -> None:
    """A tool that fails is a result, not an exception: `is_error` is part of the
    protocol and an agent has to see the text to retry. Only a transport fault is
    exceptional."""
    connector = await connect(build_server())
    out = await connector.call("does_not_exist", {})
    assert out.startswith("error:")
    assert "Unknown tool" in out
    await connector.aclose()


async def test_search_ranks_over_the_resource_listing() -> None:
    connector = await connect(build_server())
    hits = await connector.search("retrieval")
    assert [h.ref for h in hits] == ["probe:note://alpha"]
    assert hits[0].provenance.source_id == "probe"
    assert hits[0].provenance.origin == "note://alpha"
    await connector.aclose()


async def test_fetch_reads_a_resource() -> None:
    connector = await connect(build_server())
    doc = await connector.fetch("note://beta")
    assert "destroys it" in doc.body
    assert doc.ref == "probe:note://beta"
    await connector.aclose()


async def test_fetching_something_absent_says_so() -> None:
    connector = await connect(build_server())
    with pytest.raises(ConnectError, match="could not read"):
        await connector.fetch("note://nowhere")
    await connector.aclose()


# -- lifecycle --------------------------------------------------------------


async def test_a_server_that_will_not_start_fails_with_its_command() -> None:
    connector = McpConnector("bad", "this-command-does-not-exist --flag")
    with pytest.raises(ConnectError, match="could not start"):
        await connector.aopen()
    # aopen cleaned up after itself, so aclose is safe and idempotent.
    await connector.aclose()
    await connector.aclose()


async def test_an_empty_command_is_refused() -> None:
    with pytest.raises(ConnectError, match="no server command"):
        _transport("   ")


def test_a_url_goes_straight_to_the_sdk() -> None:
    assert _transport("https://example.test/mcp") == "https://example.test/mcp"


def test_a_command_becomes_a_stdio_transport() -> None:
    transport = _transport("uv run lw serve --store /tmp/x")
    assert hasattr(transport, "__aenter__")


# -- through the session ----------------------------------------------------


async def test_an_mcp_source_reaches_the_router_but_is_not_searched() -> None:
    """A tools-only server contributes tools and is skipped by an implicit search —
    the reason capabilities compose rather than being one fat Protocol."""
    session = Session()
    connector = await connect(build_server(with_resources=False), source_id="gh")
    session._connectors["gh"] = connector  # type: ignore[assignment]

    assert {t.qualified for t in session.tools()} == {
        "gh.echo",
        "gh.wipe",
        "gh.mystery",
    }
    assert await session.find("anything") == {}
    await session.aclose()


async def test_an_mcp_source_with_resources_is_searched() -> None:
    session = Session()
    session._connectors["probe"] = await connect(build_server())  # type: ignore[assignment]
    grouped = await session.find("compaction")
    assert list(grouped) == ["probe"]
    assert grouped["probe"][0].ref == "probe:note://beta"
    await session.aclose()


async def test_a_failing_body_still_closes_the_session() -> None:
    """The bug this guards: `intel stores | head -3` closed the pipe, BrokenPipeError
    skipped the cleanup at the end of the command, and the orphaned server inherited the
    pipeline's stdout — so the shell never saw EOF and hung. An MCP source is a
    subprocess; closing it is not optional."""
    from latent_intel.frontends._shared import session_scope

    closed: list[str] = []

    class Recording(McpConnector):
        async def aclose(self) -> None:
            closed.append(self.id)
            await super().aclose()

    connector = Recording("probe", build_server())
    await connector.aopen()

    with pytest.raises(BrokenPipeError):
        async with session_scope() as (session, _):
            session._connectors["probe"] = connector  # type: ignore[assignment]
            raise BrokenPipeError("head closed the pipe")

    assert closed == ["probe"], "the session must close even when the body raises"
