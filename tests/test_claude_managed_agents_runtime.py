"""`claude-managed-agents` — Claude Managed Agents behind the `Runtime` Protocol.

No request leaves this file. `client_factory` returns a fake client exposing the
`beta.agents` and `beta.environments` calls `provision` makes, so the definition, the
fingerprint and the sync bookkeeping are tested without a key that spends.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from latent_intel import config as config_module
from latent_intel import events as ev
from latent_intel import settings as settings_module
from latent_intel.agent import base as agent
from latent_intel.agent import tools as engine_tools
from latent_intel.agent.runtimes import claude_managed_agents as managed
from latent_intel.agent.runtimes.claude_managed_agents import (
    ClaudeManagedAgentsRuntime,
    fingerprint,
)
from latent_intel.models import (
    Capability,
    Descriptor,
    Effect,
    Message,
    RuntimeUnavailable,
    Skill,
    ToolSpec,
    WebScope,
)

GOLDEN = Path(__file__).parent / "fixtures" / "managed" / "definition.json"
SYNCED = {
    "agent_id": "agent_1",
    "version": 1,
    "environment_id": "env_1",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnects back off in milliseconds here, not the quarter-seconds they wait."""
    monkeypatch.setattr(managed, "BACKOFF", 0.001)
    monkeypatch.setattr(managed, "OWNER_GRACE", 0.2)


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present and meaningless; `conftest` deletes every `ANTHROPIC_*` first."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-printed-key")


class FakeClient:
    """The two resources `provision` touches, recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.beta = SimpleNamespace(
            agents=SimpleNamespace(create=self._create, update=self._update),
            environments=SimpleNamespace(
                create=self._environment, update=self._environment_update
            ),
        )

    def __call__(self) -> FakeClient:
        return self

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(("agents.create", kwargs))
        return SimpleNamespace(id="agent_1", version=1)

    async def _update(self, agent_id: str, **kwargs: Any) -> Any:
        self.calls.append(("agents.update", {"agent_id": agent_id, **kwargs}))
        return SimpleNamespace(id=agent_id, version=kwargs["version"] + 1)

    async def _environment(self, **kwargs: Any) -> Any:
        self.calls.append(("environments.create", kwargs))
        return SimpleNamespace(id="env_1")

    async def _environment_update(self, environment_id: str, **kwargs: Any) -> Any:
        self.calls.append(("environments.update", {"id": environment_id, **kwargs}))
        return SimpleNamespace(id=environment_id)


def tools() -> list[ToolSpec]:
    """Two of a source's, and the engine's `load_skill`, as `Session.tools` offers them
    whenever a deployment has skills — which is why a skill's body never reaches the
    agent's definition, only its listing line."""
    return [
        *engine_tools.specs(context()["skills"]),
        ToolSpec(
            name="wiki_search",
            source_id="design",
            description="Search the design wiki.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            effect=Effect.EXTERNAL_READ,
        ),
        ToolSpec(
            name="wiki_write",
            source_id="design",
            description="Write a page.",
            effect=Effect.LOCAL_WRITE,
        ),
    ]


def context(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sources": [
            Descriptor(
                id="design",
                kind="mcp",
                capabilities=[Capability.SEARCH, Capability.TOOLS],
            )
        ],
        "persona": "You are an archivist.",
        "persona_mode": "append",
        "skills": [
            Skill(name="citing", description="How to cite.", body="Cite inline.")
        ],
        "web": WebScope(mode="browse", allowed_domains=["arxiv.org"]),
        "name": "research",
    }
    return {**base, **changes}


# -- step 1: what it needs, and what it refuses -------------------------------


def test_it_is_registered_under_the_vendor_s_product_name() -> None:
    assert (
        agent.available_kinds()["claude-managed-agents"] is ClaudeManagedAgentsRuntime
    )


def test_the_key_is_named_never_read_out(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = ClaudeManagedAgentsRuntime(synced=SYNCED).unavailable_reason()
    assert reason and "ANTHROPIC_API_KEY" in reason
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-printed-key")
    assert "never-printed-key" not in str(
        ClaudeManagedAgentsRuntime().unavailable_reason()
    )


def test_an_sdk_too_old_for_the_api_says_to_reinstall(
    key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed, "_sdk_version", lambda: (1, 4))
    reason = ClaudeManagedAgentsRuntime(synced=SYNCED).unavailable_reason()
    assert reason and "anthropic>=1.5" in reason and "reinstall" in reason


def test_with_no_agent_created_it_says_to_sync(key: None) -> None:
    reason = ClaudeManagedAgentsRuntime().unavailable_reason()
    assert reason and "intel managed sync" in reason
    assert ClaudeManagedAgentsRuntime(synced=SYNCED).available()


def test_only_the_first_party_host_serves_it(key: None) -> None:
    reason = ClaudeManagedAgentsRuntime(
        host="foundry-anthropic", synced=SYNCED
    ).unavailable_reason()
    assert reason and "'anthropic' only" in reason


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_tool_rounds": 10}, "owns its loop"),
        ({"max_tokens": 5}, "unknown option"),
        ({"max_cost_usd": 0}, "above zero"),
    ],
)
def test_options_it_cannot_honour_are_refused_by_name(
    options: dict[str, Any], message: str
) -> None:
    with pytest.raises(RuntimeUnavailable, match=message):
        ClaudeManagedAgentsRuntime(**options)


def test_it_is_the_managed_family_and_takes_domain_lists_but_not_use_caps() -> None:
    runtime = ClaudeManagedAgentsRuntime()
    assert runtime.family() == "managed"
    assert set(runtime.host_status()) == {"anthropic"}
    assert (
        agent.web_reason(runtime, WebScope(mode="browse", blocked_domains=["x.io"]))
        is None
    )
    reason = agent.web_reason(runtime, WebScope(mode="search", max_uses=3))
    assert reason and "max_uses" in reason


@pytest.mark.anyio
async def test_a_runtime_that_cannot_run_fails_as_an_event() -> None:
    events = [
        e
        async for e in ClaudeManagedAgentsRuntime().stream(
            [Message(text="q")], [], emitter=ev.Emitter(uuid4())
        )
    ]
    assert [type(e) for e in events] == [ev.AgentFailed]
    assert events[0].kind == "runtime_unavailable"  # type: ignore[union-attr]


# -- step 2: the definition, and sync -----------------------------------------


def test_the_definition_matches_the_golden_file() -> None:
    """Regenerate by hand when the definition changes on purpose — and read the diff."""
    definition = ClaudeManagedAgentsRuntime(
        model="claude-opus-5-5", effort="high"
    ).definition(tools(), **context())
    assert definition == json.loads(GOLDEN.read_text())


def test_approval_filters_the_tools_and_the_web_is_all_the_toolset_turns_on() -> None:
    asked = ClaudeManagedAgentsRuntime().definition(tools(), **context())
    names = [t.get("name") for t in asked["tools"] if t["type"] == "custom"]
    assert "design_wiki_write" not in names and "design_wiki_search" in names
    auto = ClaudeManagedAgentsRuntime(approval="auto").definition(tools(), **context())
    assert "design_wiki_write" in [t.get("name") for t in auto["tools"]]

    [toolset] = [t for t in asked["tools"] if t["type"] == "agent_toolset_20260401"]
    assert toolset["default_config"] == {"enabled": False}
    assert [c["name"] for c in toolset["configs"]] == ["web_search", "web_fetch"]
    assert all(c["allowed_domains"] == ["arxiv.org"] for c in toolset["configs"])
    off = ClaudeManagedAgentsRuntime().definition(tools(), **context(web=WebScope()))
    assert [t for t in off["tools"] if t["type"] != "custom"][0]["configs"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"sources": []},
        {"persona": "You are a cartographer."},
        {"web": WebScope(mode="search")},
        {"skills": []},
    ],
)
def test_the_fingerprint_moves_with_what_the_agent_would_do(
    change: dict[str, Any],
) -> None:
    runtime = ClaudeManagedAgentsRuntime()
    before = fingerprint(runtime.definition(tools(), **context()))
    assert fingerprint(runtime.definition(tools(), **context(**change))) != before


def test_the_fingerprint_ignores_the_name_and_a_skill_run_by_hand() -> None:
    """A `/name` skill travels in the turn's first message, so running one must not
    make the agent stale; nor does what the deployment is called."""
    runtime = ClaudeManagedAgentsRuntime()
    before = fingerprint(runtime.definition(tools(), **context()))
    [skill] = context()["skills"]
    invoked = skill.model_copy(update={"body": "Cite inline. In footnotes."})
    after = runtime.definition(
        tools(), **context(skills=[], invoked=[invoked], name="elsewhere")
    )
    assert fingerprint(after) == before
    approved = ClaudeManagedAgentsRuntime(approval="auto")
    assert fingerprint(approved.definition(tools(), **context())) != before


@pytest.mark.anyio
async def test_the_first_sync_creates_the_agent_and_its_environment(key: None) -> None:
    client = FakeClient()
    runtime = ClaudeManagedAgentsRuntime(client_factory=client)
    definition = runtime.definition(tools(), **context())
    synced = await runtime.provision(definition)
    assert [name for name, _ in client.calls] == [
        "agents.create",
        "environments.create",
    ]
    assert client.calls[0][1] == definition
    # The service refuses a web tool's `allowed_domains` outside the environment's
    # `allowed_hosts`, so the hosts are the scope's allowed domains.
    assert client.calls[1][1]["config"] == {
        "type": "cloud",
        "networking": {"type": "limited", "allowed_hosts": ["arxiv.org"]},
    }
    assert synced == {**SYNCED, "fingerprint": fingerprint(definition)}


@pytest.mark.anyio
async def test_a_new_domain_list_updates_the_environment_with_the_agent(
    key: None,
) -> None:
    client = FakeClient()
    first = ClaudeManagedAgentsRuntime(client_factory=client)
    synced = await first.provision(first.definition(tools(), **context()))
    again = ClaudeManagedAgentsRuntime(client_factory=client, synced=synced)
    scope = WebScope(mode="search", allowed_domains=["docs.python.org", "arxiv.org"])
    await again.provision(again.definition(tools(), **context(web=scope)))
    assert [name for name, _ in client.calls][2:] == [
        "environments.update",
        "agents.update",
    ]
    assert client.calls[2][1] == {
        "id": "env_1",
        "config": {
            "type": "cloud",
            "networking": {
                "type": "limited",
                "allowed_hosts": ["arxiv.org", "docs.python.org"],
            },
        },
    }
    off = ClaudeManagedAgentsRuntime(client_factory=client, synced=synced)
    await off.provision(off.definition(tools(), **context(web=WebScope())))
    assert client.calls[-2][1]["config"]["networking"]["allowed_hosts"] == []


@pytest.mark.anyio
async def test_an_unchanged_definition_makes_no_request(key: None) -> None:
    client = FakeClient()
    definition = ClaudeManagedAgentsRuntime().definition(tools(), **context())
    synced = {**SYNCED, "fingerprint": fingerprint(definition)}
    runtime = ClaudeManagedAgentsRuntime(client_factory=client, synced=synced)
    assert await runtime.provision(definition) == synced
    assert client.calls == []


@pytest.mark.anyio
async def test_a_changed_definition_is_a_new_version_in_the_same_environment(
    key: None,
) -> None:
    client = FakeClient()
    runtime = ClaudeManagedAgentsRuntime(
        client_factory=client, synced={**SYNCED, "fingerprint": "old"}
    )
    definition = runtime.definition(tools(), **context())
    synced = await runtime.provision(definition)
    assert [name for name, _ in client.calls] == [
        "environments.update",
        "agents.update",
    ]
    assert client.calls[1][1]["agent_id"] == "agent_1"
    assert client.calls[1][1]["version"] == 1
    assert (synced["version"], synced["environment_id"]) == (2, "env_1")


@pytest.mark.anyio
async def test_a_failed_environment_update_leaves_the_agent_where_it_was(
    key: None,
) -> None:
    """The environment moves first: an agent updated ahead of a failed environment
    update would leave the overlay a version behind, and every later sync refused."""
    client = FakeClient()

    async def refused(environment_id: str, **kwargs: Any) -> Any:
        client.calls.append(("environments.update", kwargs))
        raise RuntimeError("503")

    client.beta.environments.update = refused
    runtime = ClaudeManagedAgentsRuntime(
        client_factory=client, synced={**SYNCED, "fingerprint": "old"}
    )
    with pytest.raises(RuntimeError):
        await runtime.provision(runtime.definition(tools(), **context()))
    assert [name for name, _ in client.calls] == ["environments.update"]


@pytest.mark.anyio
async def test_sync_without_a_key_makes_no_request() -> None:
    client = FakeClient()
    runtime = ClaudeManagedAgentsRuntime(client_factory=client)
    with pytest.raises(RuntimeUnavailable, match="ANTHROPIC_API_KEY"):
        await runtime.provision(runtime.definition(tools(), **context()))
    assert client.calls == []


@pytest.mark.anyio
async def test_a_turn_against_a_stale_agent_is_refused(key: None) -> None:
    runtime = ClaudeManagedAgentsRuntime(synced={**SYNCED, "fingerprint": "old"})
    events = [
        e
        async for e in runtime.stream(
            [Message(text="q")], tools(), emitter=ev.Emitter(uuid4()), **context()
        )
    ]
    assert [type(e) for e in events] == [ev.AgentFailed]
    assert events[0].kind == "managed_stale"  # type: ignore[union-attr]
    assert "intel managed sync" in events[0].remedy  # type: ignore[union-attr]

    # A pinned version runs as it is: it describes what ran, whatever is attached now.
    pinned = ClaudeManagedAgentsRuntime(
        synced={**SYNCED, "fingerprint": "old"}, agent_version=1
    )
    kinds = [
        getattr(e, "kind", None)
        async for e in pinned.stream(
            [Message(text="q")], tools(), emitter=ev.Emitter(uuid4()), **context()
        )
    ]
    assert "managed_stale" not in kinds


# -- sync through the session and the CLI --------------------------------------


def _deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / "research.yaml").write_text(
        "name: research\nagent:\n  persona: ./persona.md\n", encoding="utf-8"
    )
    (projects / "persona.md").write_text("You are an archivist.\n", encoding="utf-8")
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "research")
    settings_module.invalidate()


def test_a_dry_run_prints_the_agent_and_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: None
) -> None:
    from latent_intel.frontends.cli.main import app

    _deployment(tmp_path, monkeypatch)
    client = FakeClient()
    monkeypatch.setattr(ClaudeManagedAgentsRuntime, "_client", lambda self: client)
    result = CliRunner().invoke(app, ["managed", "sync", "--dry-run"])
    assert result.exit_code == 0, result.output
    definition = json.loads(result.stdout)
    assert definition["name"] == "latent-intel · research"
    assert "You are an archivist." in definition["system"]
    assert client.calls == []
    assert config_module.load().overlays == {}


def test_a_sync_is_kept_in_the_user_overlay_and_reaches_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: None
) -> None:
    from latent_intel.frontends.cli.main import app

    _deployment(tmp_path, monkeypatch)
    client = FakeClient()
    monkeypatch.setattr(ClaudeManagedAgentsRuntime, "_client", lambda self: client)
    result = CliRunner().invoke(app, ["managed", "sync"])
    assert result.exit_code == 0, result.output
    assert "agent_1" in result.output

    held = config_module.load().overlays["research"].runtimes
    assert held["claude-managed-agents"]["synced"]["agent_id"] == "agent_1"
    options = settings_module.load().runtime_options("claude-managed-agents")
    built = agent.build("claude-managed-agents", **options)
    assert built.available()
    # The deployment's own file is untouched: a deployment is read, never written.
    assert "synced" not in (tmp_path / "projects" / "research.yaml").read_text()

    # And syncing again with nothing changed makes no request.
    CliRunner().invoke(app, ["managed", "sync"])
    assert [name for name, _ in client.calls] == [
        "agents.create",
        "environments.create",
    ]


# -- step 3: the turn -------------------------------------------------------------
#
# A scripted server: the first step of a turn is published when the question is sent,
# each later one when a custom tool's result comes back. Every open stream sees each
# event, and `list` replays the history, which is what a reconnect reads.

import asyncio  # noqa: E402
import itertools  # noqa: E402

import anyio  # noqa: E402

_ids = itertools.count(1)


def E(kind: str, **fields: Any) -> Any:
    """An event as the SDK's models present it: attributes, and an id unless it is a
    delta, which carries the id of the event it belongs to."""
    if kind not in ("event_delta", "event_start") and "id" not in fields:
        fields["id"] = f"ev_{next(_ids)}"
    return SimpleNamespace(type=kind, **fields)


def idle(reason: str) -> Any:
    return E("session.status_idle", stop_reason=SimpleNamespace(type=reason))


def said(text: str, *, streamed: bool = True) -> list[Any]:
    """An agent message, streamed as deltas and then whole, as the API sends it."""
    message = E("agent.message", content=[SimpleNamespace(type="text", text=text)])
    if not streamed:
        return [message]
    delta = SimpleNamespace(content=SimpleNamespace(type="text", text=text))
    return [E("event_delta", event_id=message.id, delta=delta), message]


def spent(cents: int) -> Any:
    usage = SimpleNamespace(
        list_cost=SimpleNamespace(amount=str(cents), currency="USD")
    )
    return E("session.usage", usage=usage)


def request(input_tokens: int, output_tokens: int) -> Any:
    return E(
        "span.model_request_end",
        model_usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def tool_use(name: str = "notes_search", **arguments: Any) -> Any:
    return E("agent.custom_tool_use", name=name, input=arguments)


class Live:
    """One open event stream."""

    def __init__(
        self,
        server: Server,
        *,
        drop_after: int | None = None,
        end_after: int | None = None,
    ) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.drop_after = drop_after
        self.end_after = end_after
        self.closed = False
        server.lives.append(self)
        self.server = server

    async def __aenter__(self) -> Live:
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.closed = True
        self.server.lives.remove(self)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)

    def __aiter__(self) -> Live:
        return self

    async def __anext__(self) -> Any:
        if self.drop_after is not None:
            if self.drop_after == 0:
                raise ConnectionError("the stream dropped")
            self.drop_after -= 1
        if self.end_after is not None:
            if self.end_after == 0:
                raise StopAsyncIteration
            self.end_after -= 1
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class Server:
    """The session's events, published to every open stream as the script says."""

    def __init__(self, *turns: list[list[Any]], drops: list[int] | None = None) -> None:
        self.turns = [list(t) for t in turns]
        self.steps: list[list[Any]] = []
        self.lives: list[Live] = []
        self.history: list[Any] = []
        self.sent: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.drops = list(drops or [])
        #: Per display stream: end it, cleanly, after this many events.
        self.ends: list[int] = []
        self.runner_error: BaseException | None = None
        self.list_cost: Any = None
        #: False to make `send` return no data, as an SDK reply may.
        self.echo = True
        #: True to make the runner give up posting a result, as a 4xx would.
        self.unposted = False
        #: How long a tool takes inside the runner, which shields it as the SDK does.
        self.tool_seconds = 0.0

    def publish(self, *events: Any) -> None:
        for event in events:
            # Cost is the session's to report, read with `retrieve`: the SDK's stream
            # does not pass `session.usage` through.
            if event.type == "session.usage":
                self.list_cost = event.usage.list_cost
                continue
            # Deltas are the live stream's alone: the history `list` reads holds whole
            # events, as the API's does.
            if event.type not in ("event_delta", "event_start"):
                self.history.append(event)
            for live in list(self.lives):
                live.queue.put_nowait(event)

    # -- the resources ---------------------------------------------------------

    async def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return SimpleNamespace(id=f"sess_{len(self.created)}")

    async def retrieve(self, session_id: str) -> Any:
        return SimpleNamespace(usage=SimpleNamespace(list_cost=self.list_cost))

    async def stream(self, session_id: str, **kwargs: Any) -> Live:
        drop = self.drops.pop(0) if self.drops else None
        end = self.ends.pop(0) if self.ends else None
        return Live(self, drop_after=drop, end_after=end)

    async def send(self, session_id: str, *, events: list[dict[str, Any]]) -> Any:
        self.sent.extend(events)
        echoed = []
        for event in events:
            if event["type"] == "user.message":
                echo = E("user.message", content=event["content"])
                echoed.append(echo)
                self.publish(echo)
                self.steps = self.turns.pop(0)
                self.publish(*self.steps.pop(0))
            elif event["type"] == "user.custom_tool_result":
                fields = {k: v for k, v in event.items() if k != "type"}
                self.publish(E("user.custom_tool_result", **fields))
                if self.steps:
                    self.publish(*self.steps.pop(0))
        return SimpleNamespace(data=echoed if self.echo else None)

    async def list(self, session_id: str, **kwargs: Any) -> Any:
        for event in list(self.history):
            yield event

    def tool_runner(self, session_id: str, *, tools: list[Any], max_idle: Any) -> Any:
        return Runner(self, session_id, tools)

    def client(self) -> Any:
        events = SimpleNamespace(
            stream=self.stream,
            send=self.send,
            list=self.list,
            tool_runner=self.tool_runner,
        )
        sessions = SimpleNamespace(
            create=self.create, retrieve=self.retrieve, events=events
        )
        client = SimpleNamespace(beta=SimpleNamespace(sessions=sessions))

        class Context:
            async def __aenter__(self) -> Any:
                return client

            async def __aexit__(self, *_: Any) -> None:
                return None

        return Context()


class Runner:
    """Stands in for the SDK's session tool runner: each custom tool call it sees is
    run through the tool it was given, and the result posted back."""

    def __init__(self, server: Server, session_id: str, tools: list[Any]) -> None:
        self.server = server
        self.session_id = session_id
        self.tools = {tool.name: tool for tool in tools}

    async def __aiter__(self) -> Any:
        from anthropic.lib.tools import ToolError

        if self.server.runner_error is not None:
            raise self.server.runner_error
        async with Live(self.server) as live:
            for event in self.server.history:
                live.queue.put_nowait(event)
            answered = {
                e.custom_tool_use_id
                for e in self.server.history
                if e.type == "user.custom_tool_result"
            }
            async for event in live:
                if event.type != "agent.custom_tool_use" or event.id in answered:
                    continue
                answered.add(event.id)
                with anyio.CancelScope(shield=True):
                    await anyio.sleep(self.server.tool_seconds)
                    try:
                        content = await self.tools[event.name].call(event.input)
                        failed = False
                    except ToolError as exc:
                        content, failed = str(exc), True
                if self.server.unposted:
                    yield SimpleNamespace(
                        name=event.name, is_error=failed, result={}, posted=False
                    )
                    continue
                await self.server.send(
                    self.session_id,
                    events=[
                        {
                            "type": "user.custom_tool_result",
                            "custom_tool_use_id": event.id,
                            "content": [{"type": "text", "text": content}],
                            "is_error": failed,
                        }
                    ],
                )
                yield SimpleNamespace(
                    name=event.name, is_error=failed, result={}, posted=True
                )


SEARCH = ToolSpec(
    name="search",
    source_id="notes",
    description="Search the notes.",
    effect=Effect.EXTERNAL_READ,
)


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    if arguments.get("query") == "fail":
        raise RuntimeError("the index is offline")
    return "notes:tides — Tides\n  The Moon's gravity."


def synced_runtime(
    server: Server, *, sent: dict[str, Any] | None = None, **options: Any
) -> ClaudeManagedAgentsRuntime:
    """A runtime whose synced agent matches what the turn sends — `sent` is the
    context the turn will pass — so it is not stale."""
    probe = ClaudeManagedAgentsRuntime(**options)
    digest = fingerprint(probe.definition([SEARCH], **(sent or {})))
    return ClaudeManagedAgentsRuntime(
        client_factory=server.client,
        synced={**SYNCED, "fingerprint": digest},
        **options,
    )


async def turn(
    runtime: ClaudeManagedAgentsRuntime,
    messages: list[Message] | None = None,
    *,
    session: Any = None,
    **options: Any,
) -> list[ev.AgentEvent]:
    with anyio.fail_after(5):
        return [
            event
            async for event in runtime.stream(
                messages or [Message(text="What causes the tides?")],
                [SEARCH],
                emitter=ev.Emitter(session or uuid4()),
                call_tool=_router,
                **options,
            )
        ]


def terminal(events: list[ev.AgentEvent]) -> ev.AgentEvent:
    ends = [e for e in events if isinstance(e, (ev.AgentCompleted, ev.AgentFailed))]
    assert len(ends) == 1, ends
    assert events[-1] is ends[0]
    return ends[0]


@pytest.mark.anyio
async def test_text_streams_once_and_the_turn_completes_with_usage_and_cost(
    key: None,
) -> None:
    server = Server(
        [
            [
                *said("The Moon's gravity."),
                request(900, 40),
                request(300, 20),
                spent(12),
                idle("end_turn"),
            ]
        ]
    )
    events = await turn(synced_runtime(server))
    tokens = [e.text for e in events if isinstance(e, ev.AssistantToken)]
    assert tokens == ["The Moon's gravity."]
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert (done.text, done.streamed, done.stopped) == (
        "The Moon's gravity.",
        True,
        None,
    )
    assert (done.usage["input_tokens"], done.usage["output_tokens"]) == (1200, 60)
    assert done.cost_usd == pytest.approx(0.12)
    # Attached before asking: the question was sent after the stream opened.
    assert server.sent[0]["type"] == "user.message"
    assert server.sent[0]["content"][-1] == {
        "type": "text",
        "text": "What causes the tides?",
    }


@pytest.mark.anyio
async def test_a_custom_tool_call_is_routed_paired_and_answered(key: None) -> None:
    server = Server(
        [
            [tool_use(query="tides"), idle("requires_action")],
            [*said("The Moon's gravity (notes:tides)."), idle("end_turn")],
        ]
    )
    events = await turn(synced_runtime(server))
    started = [e for e in events if isinstance(e, ev.ToolStarted)]
    results = [e for e in events if isinstance(e, ev.ToolResult)]
    assert [(e.tool, e.source_id) for e in started] == [("search", "notes")]
    assert results[0].ok and results[0].parent_id == started[0].event_id
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "The Moon's gravity (notes:tides)."
    assert events.index(results[0]) < events.index(done)
    [answer] = [e for e in server.sent if e["type"] == "user.custom_tool_result"]
    assert "notes:tides" in answer["content"][0]["text"]
    assert answer["is_error"] is False


@pytest.mark.anyio
async def test_a_failing_tool_is_an_error_result_the_model_sees(key: None) -> None:
    server = Server(
        [
            [tool_use(query="fail"), idle("requires_action")],
            [*said("The notes are offline."), idle("end_turn")],
        ]
    )
    events = await turn(synced_runtime(server))
    [result] = [e for e in events if isinstance(e, ev.ToolResult)]
    assert not result.ok and "offline" in result.error
    [answer] = [e for e in server.sent if e["type"] == "user.custom_tool_result"]
    assert answer["is_error"] is True
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_a_stream_dropped_mid_tool_reconnects_without_a_duplicate(
    key: None,
) -> None:
    """The display stream drops after the tool call; the reconnect reads the history
    from the question on, so the pair and the answer each arrive once."""
    server = Server(
        [
            [tool_use(query="tides"), idle("requires_action")],
            [*said("The Moon's gravity."), idle("end_turn")],
        ],
        drops=[2],
    )
    events = await turn(synced_runtime(server))
    assert len([e for e in events if isinstance(e, ev.ToolStarted)]) == 1
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 1
    tokens = [e.text for e in events if isinstance(e, ev.AssistantToken)]
    assert tokens == ["The Moon's gravity."]
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_the_budget_ending_a_turn_is_the_answer_so_far(key: None) -> None:
    server = Server([[*said("Two of three found."), spent(50), idle("budget_reached")]])
    done = terminal(await turn(synced_runtime(server, max_cost_usd=0.5)))
    assert isinstance(done, ev.AgentCompleted)
    assert (done.text, done.stopped, done.cost_usd) == (
        "Two of three found.",
        "cost",
        0.5,
    )
    assert server.created[0]["budget"] == {
        "type": "limit",
        "max_list_cost": {"amount": "50", "currency": "USD"},
    }

    bare = Server([[idle("budget_reached")]])
    failed = terminal(await turn(synced_runtime(bare, max_cost_usd=0.5)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "cost"


@pytest.mark.anyio
async def test_a_terminated_session_fails_once_with_its_error(key: None) -> None:
    error = SimpleNamespace(
        type="model_overloaded_error", message="the model is overloaded"
    )
    server = Server([[E("session.error", error=error), E("session.status_terminated")]])
    failed = terminal(await turn(synced_runtime(server)))
    assert isinstance(failed, ev.AgentFailed)
    assert (failed.kind, failed.message) == (
        "managed_terminated",
        "the model is overloaded",
    )


@pytest.mark.anyio
async def test_a_web_search_the_service_ran_is_the_web_s_pair(key: None) -> None:
    use = E("agent.tool_use", name="web_search", input={"query": "tides"})
    result = E(
        "agent.tool_result",
        tool_use_id=use.id,
        is_error=False,
        content=[
            SimpleNamespace(
                type="search_result",
                title="Tide",
                source="https://en.wikipedia.org/wiki/Tide",
                content=[],
            )
        ],
    )
    server = Server([[use, result, *said("Tides follow the Moon."), idle("end_turn")]])
    scope = {"web": WebScope(mode="search")}
    events = await turn(synced_runtime(server, sent=scope), **scope)
    [started] = [e for e in events if isinstance(e, ev.ToolStarted)]
    [ended] = [e for e in events if isinstance(e, ev.ToolResult)]
    assert (started.tool, started.source_id, started.effect) == (
        "web_search",
        "web",
        "external_read",
    )
    assert ended.refs == ["web:https://en.wikipedia.org/wiki/Tide"]
    assert ended.output == "Tide — https://en.wikipedia.org/wiki/Tide"
    assert ended.duration_ms is None


@pytest.mark.anyio
async def test_a_follow_up_continues_the_session_and_costs_only_its_own_turn(
    key: None,
) -> None:
    server = Server(
        [[*said("The Moon."), spent(10), idle("end_turn")]],
        [[*said("Twice a day."), spent(25), idle("end_turn")]],
    )
    runtime = synced_runtime(server)
    session = uuid4()
    await turn(runtime, session=session)
    done = terminal(
        await turn(
            runtime,
            [
                Message(text="What causes the tides?"),
                Message(role="assistant", text="The Moon."),
                Message(text="How often?"),
            ],
            session=session,
        )
    )
    assert isinstance(done, ev.AgentCompleted)
    assert len(server.created) == 1
    assert done.cost_usd == pytest.approx(0.15)
    asked = [e for e in server.sent if e["type"] == "user.message"]
    assert asked[-1]["content"][-1]["text"] == "How often?"


@pytest.mark.anyio
async def test_a_skill_run_by_hand_travels_ahead_of_the_question(key: None) -> None:
    server = Server([[*said("Done."), idle("end_turn")]])
    skill = Skill(name="citing", description="How to cite.", body="Cite inline.")
    await turn(synced_runtime(server, sent={"invoked": [skill]}), invoked=[skill])
    content = server.sent[0]["content"]
    assert [b["text"] for b in content] == ["Cite inline.", "What causes the tides?"]


@pytest.mark.anyio
async def test_a_turn_that_fails_interrupts_the_session(key: None) -> None:
    # The first stream drops after one event and every reopen at once, so the turn
    # runs out of reconnects with part of an answer in hand.
    server = Server(
        [[*said("Partly"), E("session.status_running")]], drops=[1] + [0] * 10
    )
    failed = terminal(await turn(synced_runtime(server)))
    assert isinstance(failed, ev.AgentFailed)
    assert server.sent[-1] == {"type": "user.interrupt"}


@pytest.mark.anyio
async def test_a_runner_that_dies_ends_the_turn_instead_of_waiting(key: None) -> None:
    server = Server([[tool_use(query="tides"), idle("requires_action")]])
    server.runner_error = RuntimeError("the runner could not attach")
    failed = terminal(await turn(synced_runtime(server)))
    assert isinstance(failed, ev.AgentFailed)
    assert "could not attach" in failed.message


@pytest.mark.parametrize("anyio_backend", ["trio"])
@pytest.mark.anyio
async def test_under_another_event_loop_it_refuses_rather_than_hang(
    key: None, anyio_backend: str
) -> None:
    """The tool runner lives in an asyncio task of its own, as an MCP source does, so
    under trio the turn says so before anything is sent."""
    server = Server([[*said("unused"), idle("end_turn")]])
    events = [
        e
        async for e in synced_runtime(server).stream(
            [Message(text="q")],
            [SEARCH],
            emitter=ev.Emitter(uuid4()),
            call_tool=_router,
        )
    ]
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed) and "asyncio" in failed.message
    assert server.sent == []


# -- from review -------------------------------------------------------------------


def test_what_counts_as_a_dropped_connection_is_what_the_sdk_retries() -> None:
    """A stream being read raises the transport's own error, unwrapped by the SDK."""
    import anthropic
    import httpx2

    request = httpx2.Request("GET", "https://api.anthropic.com/v1/sessions")
    assert managed._transient(httpx2.RemoteProtocolError("peer closed"))
    assert managed._transient(httpx2.ReadError("reset"))
    assert managed._transient(anthropic.APIConnectionError(request=request))

    def status(code: int) -> Exception:
        response = httpx2.Response(code, request=request)
        return anthropic.APIStatusError("x", response=response, body=None)

    assert managed._transient(status(503)) and managed._transient(status(429))
    assert not managed._transient(status(400))
    assert not managed._transient(RuntimeError("a bug"))


@pytest.mark.anyio
async def test_a_message_s_whole_text_wins_over_deltas_that_stopped_early(
    key: None,
) -> None:
    """Deltas are best-effort; the buffered event carries the whole message."""
    message = E(
        "agent.message",
        content=[SimpleNamespace(type="text", text="The Moon's gravity.")],
    )
    delta = SimpleNamespace(content=SimpleNamespace(type="text", text="The Moon"))
    server = Server(
        [
            [
                E("event_delta", event_id=message.id, delta=delta),
                message,
                idle("end_turn"),
            ]
        ]
    )
    events = await turn(synced_runtime(server))
    tokens = [e.text for e in events if isinstance(e, ev.AssistantToken)]
    assert "".join(tokens) == "The Moon's gravity."
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted) and done.text == "The Moon's gravity."


@pytest.mark.anyio
async def test_deltas_lost_to_a_dropped_stream_come_back_whole(key: None) -> None:
    message = E(
        "agent.message",
        content=[SimpleNamespace(type="text", text="The Moon's gravity.")],
    )
    first = SimpleNamespace(content=SimpleNamespace(type="text", text="The Moon"))
    rest = SimpleNamespace(content=SimpleNamespace(type="text", text="'s gravity."))
    server = Server(
        [
            [
                E("event_delta", event_id=message.id, delta=first),
                E("event_delta", event_id=message.id, delta=rest),
                message,
                idle("end_turn"),
            ]
        ],
        drops=[2],  # the echoed question and the first delta, then the drop
    )
    done = terminal(await turn(synced_runtime(server)))
    assert isinstance(done, ev.AgentCompleted) and done.text == "The Moon's gravity."


@pytest.mark.anyio
async def test_a_stream_that_keeps_closing_is_reopened_while_it_makes_progress(
    key: None,
) -> None:
    """A proxy closing an idle stream is not a failure: each reopen that delivers
    something new resets the count."""
    server = Server(
        [
            [
                *said("One.", streamed=False),
                *said("Two.", streamed=False),
                *said("Three.", streamed=False),
                request(10, 2),
                request(10, 2),
                idle("end_turn"),
            ]
        ]
    )
    server.ends = [1] * 12
    done = terminal(await turn(synced_runtime(server)))
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "One.\n\nTwo.\n\nThree."


@pytest.mark.anyio
async def test_a_question_sent_without_an_echo_still_replays_after_a_drop(
    key: None,
) -> None:
    """A send reply may carry no data; the replay then starts from the last question
    in the history rather than skipping what arrived in the gap."""
    server = Server([[*said("The Moon.", streamed=False), idle("end_turn")]], drops=[1])
    server.echo = False
    done = terminal(await turn(synced_runtime(server)))
    assert isinstance(done, ev.AgentCompleted) and done.text == "The Moon."


@pytest.mark.anyio
async def test_a_result_the_runner_could_not_post_ends_the_turn(key: None) -> None:
    server = Server([[tool_use(query="tides"), idle("requires_action")]])
    server.unposted = True
    failed = terminal(await turn(synced_runtime(server)))
    assert isinstance(failed, ev.AgentFailed)
    assert "could not be posted" in failed.message


@pytest.mark.anyio
async def test_a_cancel_interrupts_at_once_and_does_not_wait_out_a_tool(
    key: None,
) -> None:
    server = Server([[tool_use(query="tides"), idle("requires_action")]])
    server.tool_seconds = 3.0
    runtime = synced_runtime(server)

    async def ask() -> None:
        async for _ in runtime.stream(
            [Message(text="q")],
            [SEARCH],
            emitter=ev.Emitter(uuid4()),
            call_tool=_router,
        ):
            pass

    began = asyncio.get_running_loop().time()
    with anyio.move_on_after(0.3):
        await ask()
    assert asyncio.get_running_loop().time() - began < 1.5
    assert {"type": "user.interrupt"} in server.sent


@pytest.mark.anyio
async def test_the_cost_is_none_when_the_session_cannot_be_read(
    key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server([[*said("The Moon."), spent(12), idle("end_turn")]])

    async def unreadable(session_id: str) -> Any:
        raise RuntimeError("the read failed")

    monkeypatch.setattr(server, "retrieve", unreadable)
    done = terminal(await turn(synced_runtime(server)))
    assert isinstance(done, ev.AgentCompleted)
    assert (done.text, done.cost_usd) == ("The Moon.", None)


@pytest.mark.anyio
async def test_after_a_budget_stop_the_next_question_starts_a_new_session(
    key: None,
) -> None:
    """Its budget is spent, so continuing it would stop again at once."""
    server = Server(
        [[*said("Partly."), spent(50), idle("budget_reached")]],
        [[*said("Fresh."), idle("end_turn")]],
    )
    runtime = synced_runtime(server, max_cost_usd=0.5)
    session = uuid4()
    await turn(runtime, session=session)
    await turn(
        runtime,
        [
            Message(text="What causes the tides?"),
            Message(role="assistant", text="Partly."),
            Message(text="Go on."),
        ],
        session=session,
    )
    assert len(server.created) == 2


@pytest.mark.anyio
async def test_two_tools_that_fold_to_one_name_fail_as_an_event(key: None) -> None:
    clash = [
        ToolSpec(
            name="search", source_id=source, description="", effect=Effect.EXTERNAL_READ
        )
        for source in ("my wiki", "my_wiki")
    ]
    runtime = ClaudeManagedAgentsRuntime(synced={**SYNCED, "fingerprint": "x"})
    events = [
        e
        async for e in runtime.stream(
            [Message(text="q")], clash, emitter=ev.Emitter(uuid4())
        )
    ]
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "tool_name_collision"


def test_a_deployment_named_apart_from_its_file_finds_what_sync_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: None
) -> None:
    """The overlay is kept under the name the deployment is chosen by — its file's —
    whatever its `name:` says, so a turn finds the agent sync created."""
    from latent_intel.frontends.cli.main import app

    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / "research.yaml").write_text("name: Research Desk\n", encoding="utf-8")
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "research")
    settings_module.invalidate()
    monkeypatch.setattr(
        ClaudeManagedAgentsRuntime, "_client", lambda self: FakeClient()
    )
    result = CliRunner().invoke(app, ["managed", "sync"])
    assert result.exit_code == 0, result.output
    options = settings_module.load().runtime_options("claude-managed-agents")
    assert agent.build("claude-managed-agents", **options).available()


def test_only_what_sync_recorded_is_read_from_the_overlay(key: None) -> None:
    """The overlay is not a second place to set a runtime's options."""
    config = config_module.load()
    config.overlay("default").runtimes["claude-managed-agents"] = {
        "synced": SYNCED,
        "model": "claude-from-the-overlay",
    }
    config_module.save(config)
    options = settings_module.load().runtime_options("claude-managed-agents")
    assert options == {"synced": SYNCED}


@pytest.mark.anyio
async def test_a_refusal_is_one_failure_with_the_service_s_reason(key: None) -> None:
    """`refusal` arrived in `anthropic` 1.10, with `stop_details` beside it."""
    stopped = idle("refusal")
    stopped.stop_details = SimpleNamespace(
        type="refusal", category="general_harms", explanation="it asked for harm"
    )
    server = Server([[stopped]])
    failed = terminal(await turn(synced_runtime(server)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "refusal"
    assert failed.message == "the model declined to answer — it asked for harm"
    assert failed.remedy == "rephrase the question"
