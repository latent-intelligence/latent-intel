"""The MCP connector, against real servers: one in-process, one spawned.

`Client` accepts a server object directly, so most of these exercise the actual protocol
— initialize, list, call, read — rather than a mock of it. `env` and `cwd` only mean
anything across a spawn, so those run `fixtures/mcp_probe_server.py` as a subprocess and
ask it what it received. No network, no store, no model.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import types
from mcp.server import MCPServer

from latent_intel import events as ev
from latent_intel.connectors import base as connectors
from latent_intel.connectors.mcp import (
    McpConnector,
    _effect_of,
    _transport,
    read_mcp_config,
)
from latent_intel.models import Capability, ConnectError, Effect, SourceRequest
from latent_intel.session import Session

PROBE = Path(__file__).parent / "fixtures" / "mcp_probe_server.py"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_server(
    *,
    with_resources: bool = True,
    instructions: str = "A probe server.",
    name: str = "probe",
) -> MCPServer:
    """A server with one honestly-annotated tool, one destructive one, and one that
    declares nothing — the three cases the effect mapping has to get right."""
    server = MCPServer(name, instructions=instructions)

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


def probe_command() -> str:
    """The probe server as a command line, which is the only way to test a spawn."""
    return shlex.join([sys.executable, str(PROBE)])


async def probe(**options: Any) -> dict[str, Any]:
    """Start the probe server in a subprocess and ask it what it got."""
    connector = McpConnector("probe", probe_command(), **options)
    await connector.aopen()
    try:
        reported: dict[str, Any] = json.loads(await connector.call("whoami", {}))
        return reported
    finally:
        await connector.aclose()


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


# -- env and cwd ------------------------------------------------------------


async def test_a_named_variable_is_forwarded_and_an_unnamed_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK starts a child with six variables and nothing else, so forwarding by
    name is what makes a server that runs under another host run here — and naming one
    must not smuggle in the rest of the parent's environment."""
    monkeypatch.setenv("PROBE_FORWARDED", "yes")
    monkeypatch.setenv("PROBE_UNNAMED", "no")
    assert (await probe(env=["PROBE_FORWARDED"]))["env"] == {"PROBE_FORWARDED": "yes"}


async def test_a_literal_reaches_the_child_verbatim() -> None:
    """`NAME=value` is for what is not a secret — a region, a data path."""
    reported = await probe(env="PROBE_LITERAL=fixed")
    assert reported["env"] == {"PROBE_LITERAL": "fixed"}


def test_an_unset_name_is_refused_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject what you cannot honour: the server would otherwise start without it and
    fail somewhere far less readable."""
    monkeypatch.delenv("PROBE_MISSING", raising=False)
    with pytest.raises(ConnectError, match="env PROBE_MISSING is not set"):
        McpConnector("probe", "some-server", env=["PROBE_MISSING"])


async def test_cwd_is_where_the_server_starts(tmp_path: Path) -> None:
    reported = await probe(cwd=str(tmp_path))
    assert Path(reported["cwd"]).resolve() == tmp_path.resolve()


def test_a_cwd_that_does_not_exist_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConnectError, match="is not a directory"):
        McpConnector("probe", "some-server", cwd=str(tmp_path / "nowhere"))


def test_server_spec_carries_cwd_and_literals_but_never_a_forwarded_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block reaches a subprocess agent on its command line, where a forwarded
    value would show in `ps` — and that agent already has it in its own environment."""
    monkeypatch.setenv("PROBE_FORWARDED", "secret")
    connector = McpConnector(
        "probe",
        "some-server --flag",
        env=["PROBE_FORWARDED", "PROBE_LITERAL=fixed"],
        cwd=str(tmp_path),
    )
    assert connector.server_spec() == {
        "command": "some-server",
        "args": ["--flag"],
        "cwd": str(tmp_path),
        "env": {"PROBE_LITERAL": "fixed"},
    }


# -- reading another host's registration ------------------------------------


def write_config(tmp_path: Path, body: dict[str, Any]) -> Path:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_all_four_keys_become_the_row_a_hand_written_source_would_have(
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        {
            "mcpServers": {
                "records": {
                    "command": "records-mcp",
                    "args": ["--store", "/tmp/store"],
                    "env": {"AWS_REGION": "${AWS_REGION}", "MODE": "local"},
                    "cwd": "./server",
                }
            }
        },
    )
    name, target, options = read_mcp_config(path)
    assert name == "records"
    assert target == "records-mcp --store /tmp/store"
    assert sorted(options["env"]) == ["AWS_REGION", "MODE=local"]
    # Against the file's directory: by the time the recorded row is read, nobody
    # remembers where the file was.
    assert options["cwd"] == str(tmp_path / "server")


def test_a_url_server_becomes_a_url_target(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, {"servers": {"remote": {"url": "https://example.test/mcp"}}}
    )
    assert read_mcp_config(path) == ("remote", "https://example.test/mcp", {})


def test_several_servers_and_no_name_lists_them(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"mcpServers": {"records": {"command": "a"}, "notes": {"command": "b"}}},
    )
    with pytest.raises(ConnectError, match="notes, records"):
        read_mcp_config(path)
    with pytest.raises(ConnectError, match="notes, records"):
        read_mcp_config(path, "absent")


def test_an_expansion_only_the_other_host_understands_is_refused(
    tmp_path: Path,
) -> None:
    """`${OTHER}` would have to be forwarded under a name it does not have, and
    `${input:…}` is a prompt we never saw. Passing the text through would hand the
    server a literal `${…}` and call it configured."""
    path = write_config(
        tmp_path,
        {"servers": {"records": {"command": "a", "env": {"REGION": "${OTHER}"}}}},
    )
    with pytest.raises(ConnectError, match="only the host that wrote this file"):
        read_mcp_config(path)


def test_headers_are_refused_rather_than_dropped(tmp_path: Path) -> None:
    """A credential silently not sent is a server that answers unauthorised."""
    path = write_config(
        tmp_path,
        {"servers": {"remote": {"url": "https://example.test/mcp", "headers": {}}}},
    )
    with pytest.raises(ConnectError, match="headers are not supported"):
        read_mcp_config(path)


def test_a_file_with_neither_root_key_says_which_it_looked_for(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"inputs": []})
    with pytest.raises(ConnectError, match="`mcpServers` and no `servers`"):
        read_mcp_config(path)


# -- lifecycle --------------------------------------------------------------


async def test_a_server_that_dies_on_startup_reports_its_own_stderr(
    tmp_path: Path,
) -> None:
    """`Connection closed` is the SDK's account of the pipe. What a reader needs is the
    sentence the server printed before it went."""
    script = tmp_path / "dies.py"
    script.write_text(
        "import sys\nsys.stderr.write('PROBE_BACKEND is not configured\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    connector = McpConnector("bad", shlex.join([sys.executable, str(script)]))
    with pytest.raises(ConnectError) as caught:
        await connector.aopen()
    assert "PROBE_BACKEND is not configured" in str(caught.value)
    await connector.aclose()


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


async def test_the_server_s_own_instructions_describe_the_source() -> None:
    """What the store is about reaches the system prompt, as it did for the wiki."""
    connector = await connect(build_server())
    try:
        described = connector.describe()
    finally:
        await connector.aclose()
    assert described.detail["description"] == "A probe server."
    assert described.title == "probe"


def test_an_option_the_server_cannot_take_is_refused() -> None:
    """`context:` beside a wiki configured its reader once; a server configures
    itself, so the option would sit there meaning nothing."""
    with pytest.raises(ConnectError, match="context.*would configure nothing"):
        connectors.build("mcp", "w", "lw serve --store x", context="/ctx")


async def test_a_server_s_instructions_are_bounded() -> None:
    """They reach the system prompt, and a third-party server does not get to write
    as much of it as it likes."""
    connector = await connect(build_server(instructions="x" * 5_000))
    try:
        described = connector.describe()
    finally:
        await connector.aclose()
    assert len(described.detail["description"]) == 500


async def test_a_filter_the_listing_cannot_apply_is_refused() -> None:
    """Reject what you cannot honour: a `--kind` that silently did nothing would
    return results the caller believes were filtered."""
    connector = await connect(build_server())
    try:
        assert await connector.search("retrieval", type=None)
        with pytest.raises(ConnectError, match="type"):
            await connector.search("retrieval", type="concept")
    finally:
        await connector.aclose()


async def test_a_tool_s_input_schema_reaches_the_spec() -> None:
    """The schema is what tells a model a tool's parameters; an empty one hides them."""
    connector = await connect(build_server())
    try:
        echo = next(t for t in connector.tools() if t.name == "echo")
    finally:
        await connector.aclose()
    assert echo.input_schema["properties"]["text"]["type"] == "string"
    assert echo.input_schema["required"] == ["text"]


# -- refs in tool output ----------------------------------------------------


class CallingRuntime:
    """A turn that runs one tool through the session, so the refs on its result are the
    session's reading of what a real server printed."""

    id = "fake"
    model = "fake-model-1"

    def __init__(self, source_id: str, tool: str, arguments: dict[str, Any]) -> None:
        self.source_id, self.tool, self.arguments = source_id, tool, arguments

    def available(self) -> bool:
        return True

    async def stream(self, messages, tools, *, emitter, call_tool, **options):  # type: ignore[no-untyped-def]
        started = emitter.emit(
            ev.ToolStarted, tool=self.tool, source_id=self.source_id, effect="none"
        )
        yield started
        yield emitter.nested(started).emit(
            ev.ToolResult,
            tool=self.tool,
            source_id=self.source_id,
            output=await call_tool(self.source_id, self.tool, self.arguments),
        )
        yield emitter.emit(ev.AgentCompleted, text="")


async def result_of(session: Session) -> ev.ToolResult:
    (result,) = [e async for e in session.ask("q") if isinstance(e, ev.ToolResult)]
    await session.aclose()
    return result


async def test_a_server_s_own_name_is_swapped_for_the_label_in_its_output() -> None:
    """The server names its keys under its own name and is never told its label; the
    session swaps one for the other, and only where the name stands on its own."""
    text = "notes:alpha, xnotes:beta, (notes:gamma). notes:alpha"
    session = Session(runtime=CallingRuntime("probe", "echo", {"text": text}))
    session._connectors["probe"] = await connect(build_server(name="notes"))  # type: ignore[assignment]
    result = await result_of(session)
    assert "probe:" not in result.output
    assert result.refs == ["probe:alpha", "probe:gamma"]


async def test_a_peer_ref_counts_only_when_that_peer_is_attached() -> None:
    """A link into a peer store is a ref when a server of that name is attached, under
    its label; a name nothing attached answers to stays text."""
    text = "pond:alpha and elsewhere:beta"
    session = Session(runtime=CallingRuntime("probe", "echo", {"text": text}))
    probe = await connect(build_server(name="notes"))
    peer = await connect(build_server(name="pond"), "peer")
    session._connectors.update(probe=probe, peer=peer)  # type: ignore[arg-type]
    assert (await result_of(session)).refs == ["peer:alpha"]


async def test_bare_keys_in_a_server_s_output_are_not_refs() -> None:
    """A bare word could be any word: only a name the server qualified is a ref."""
    session = Session(runtime=CallingRuntime("gh", "echo", {"text": "alpha, note"}))
    session._connectors["gh"] = await connect(build_server(name="notes"), "gh")  # type: ignore[assignment]
    assert (await result_of(session)).refs == []


# -- teardown ---------------------------------------------------------------
#
# Each stdio server is a subprocess behind an anyio cancel scope. anyio wants a scope
# exited by the task that entered it, in reverse order of entry, so these attach real
# servers (the probe, no extras) and close them every way a session can.


def children() -> set[int]:
    """This process's live child processes, so a test can say none was left behind."""
    out = subprocess.run(
        ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, check=False
    ).stdout
    return {int(pid) for pid in out.split()}


def tasks() -> int:
    return len(asyncio.all_tasks())


async def attach(session: Session, *names: str) -> None:
    for name in names:
        await session.connect(probe_command(), kind="mcp", source_id=name)


@pytest.mark.parametrize("count", [2, 3])
async def test_a_session_with_several_stdio_servers_closes(count: int) -> None:
    before = children()
    session = Session()
    await attach(session, *(f"s{i}" for i in range(count)))
    await session.aclose()
    assert session.sources() == []
    assert children() <= before


@pytest.mark.parametrize("count", [2, 3])
async def test_the_first_of_several_stdio_servers_disconnects(count: int) -> None:
    session = Session()
    await attach(session, *(f"s{i}" for i in range(count)))
    await session.disconnect("s0")
    assert [d.id for d in session.sources()] == [f"s{i}" for i in range(1, count)]
    assert json.loads(await session._connectors["s1"].call("whoami", {}))  # type: ignore[attr-defined]
    await session.aclose()


@pytest.mark.parametrize("count", [2, 3])
async def test_a_duplicate_opened_before_others_is_closed(count: int) -> None:
    """`connect_many` opens every request, then closes the one whose id is taken —
    while the sources opened after it are still open."""
    session = Session()
    await attach(session, "s0")
    failures = await session.connect_many(
        [
            SourceRequest(spec=probe_command(), kind="mcp", source_id=f"s{i}")
            for i in range(count)
        ]
    )
    assert failures == ["s0: already connected"]
    assert [d.id for d in session.sources()] == [f"s{i}" for i in range(count)]
    await session.aclose()


async def test_five_servers_close_in_any_order() -> None:
    before = children()
    names = [f"s{i}" for i in range(5)]
    session = Session()
    await attach(session, *names)
    random.Random(7).shuffle(names)
    for name in names[:3]:
        await session.disconnect(name)
    assert {d.id for d in session.sources()} == set(names[3:])
    await session.aclose()
    assert session.sources() == []
    assert children() <= before


async def test_a_server_that_fails_to_start_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    script = tmp_path / "dies.py"
    script.write_text(
        "import sys\nsys.stderr.write('PROBE_BACKEND is not configured\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    before_children, before_tasks = children(), tasks()
    connector = McpConnector("bad", shlex.join([sys.executable, str(script)]))
    with pytest.raises(ConnectError, match="PROBE_BACKEND is not configured"):
        await connector.aopen()
    assert children() <= before_children
    assert tasks() == before_tasks


async def test_cancelling_a_server_while_it_starts_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """Ctrl-C while a slow server starts."""
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    before_children, before_tasks = children(), tasks()
    connector = McpConnector("slow", shlex.join([sys.executable, str(script)]))
    with anyio.move_on_after(0.5) as scope:
        await connector.aopen()
    assert scope.cancelled_caught
    assert children() <= before_children
    assert tasks() == before_tasks
    await connector.aclose()


async def test_a_server_that_dies_mid_session_fails_its_own_calls_only() -> None:
    session = Session()
    await attach(session, "dying", "alive")
    dying, alive = session._connectors["dying"], session._connectors["alive"]
    with anyio.fail_after(15):
        with pytest.raises(ConnectError):
            await dying.call("die", {})  # type: ignore[attr-defined]
        with pytest.raises(ConnectError):
            await dying.call("whoami", {})  # type: ignore[attr-defined]
        assert json.loads(await alive.call("whoami", {}))  # type: ignore[attr-defined]
        await session.aclose()
    assert session.sources() == []


class StuckOnExit:
    """The probe over stdio, behind a transport that ignores the request to stop and
    closes its server once cancelled."""

    def __init__(self) -> None:
        self.inner = _transport(probe_command())

    async def __aenter__(self) -> Any:
        return await self.inner.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await anyio.sleep_forever()
        await self.inner.__aexit__(None, None, None)


async def test_a_server_that_will_not_stop_cannot_hang_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("latent_intel.connectors.mcp.CLOSE_TIMEOUT", 0.3)
    before_children, before_tasks = children(), tasks()
    connector = McpConnector("stuck", StuckOnExit())
    await connector.aopen()
    with anyio.fail_after(5):
        await connector.aclose()
        await connector.aclose()
    assert children() <= before_children
    assert tasks() == before_tasks
    assert "did not close cleanly" not in caplog.text


async def test_hung_servers_cost_one_bounded_wait_not_one_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("latent_intel.connectors.mcp.CLOSE_TIMEOUT", 0.5)
    session = Session()
    for name in ("a", "b", "c"):
        connector = McpConnector(name, StuckOnExit())
        await connector.aopen()
        session._connectors[name] = connector  # type: ignore[assignment]
    started = time.monotonic()
    with anyio.fail_after(10):
        await session.aclose()
    # One wait each, in turn, would be three CLOSE_TIMEOUTs.
    assert time.monotonic() - started < 1.45


async def test_a_failure_before_the_server_starts_is_a_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a source that looks connected and answers every call with no reason."""

    def no_space(*args: Any, **kwargs: Any) -> Any:
        raise OSError("No space left on device")

    monkeypatch.setattr("tempfile.TemporaryFile", no_space)
    connector = McpConnector("probe", probe_command())
    with pytest.raises(ConnectError, match="No space left on device"):
        await connector.aopen()


class SlowToExit(StuckOnExit):
    """A transport that stops its server, then ignores every cancel and ends on its
    own a second later."""

    async def __aexit__(self, *exc: Any) -> None:
        await self.inner.__aexit__(None, None, None)
        end = time.monotonic() + 1.0
        while (left := end - time.monotonic()) > 0:
            try:
                await asyncio.sleep(left)
            except asyncio.CancelledError:
                continue


async def test_a_server_left_running_is_named_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("latent_intel.connectors.mcp.CLOSE_TIMEOUT", 0.2)
    connector = McpConnector("slow", SlowToExit())
    await connector.aopen()
    await connector.aclose()
    assert "slow" in caplog.text and "did not stop" in caplog.text
    await anyio.sleep(1.5)


class FailsToClose(StuckOnExit):
    async def __aexit__(self, *exc: Any) -> None:
        await self.inner.__aexit__(None, None, None)
        raise OSError("transport would not close")


async def test_a_server_that_closes_with_an_error_is_named_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connector = McpConnector("broken", FailsToClose())
    await connector.aopen()
    await connector.aclose()
    assert "broken: the server did not close cleanly" in caplog.text
    assert "transport would not close" in caplog.text


@pytest.mark.parametrize("anyio_backend", ["trio"])
async def test_an_mcp_source_refuses_an_event_loop_other_than_asyncio() -> None:
    """The owner task is an asyncio task. Refused before anything starts."""
    before = children()
    connector = McpConnector("probe", probe_command())
    with pytest.raises(ConnectError, match="need the asyncio event loop"):
        await connector.aopen()
    assert children() <= before
    assert connector._owner is None
    await connector.aclose()
