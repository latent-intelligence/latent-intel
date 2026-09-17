"""The API every frontend imports, and the only one they may.

Two shapes over one implementation:

- **Typed methods** for ordinary Python callers. `doc = await session.fetch(ref)`
  returns a document. Consuming an event stream to recover a return value would be a tax
  on every script and notebook that ever uses this.
- **`run(command)`** for frontends and transports, returning an envelope-stamped
  `AsyncIterator[AgentEvent]`. This is what a CLI renders, what the shell streams, and
  what an HTTP transport will serialize.

The typed methods do the work; `run` wraps them. One implementation, so the two shapes
cannot drift.

**Search groups by source and never merges.** `find` returns `{source_id: [Hit, ...]}`
and `run(Find(...))` emits one `RetrievalResult` per source. A lexical count of 12.0 and
a cosine of 0.81 are different quantities; one ordered list would assert a comparison
neither number supports, and the first time the cosine sorted last it would be believed.
Grouping is structural here so no renderer has to remember it.

**Failures inside `run` become events, not exceptions.** A frontend iterating a stream
over a socket cannot catch an exception raised inside the generator. Anything a person
should see travels as `AgentFailed`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID, uuid4

import anyio

from . import events as ev
from . import registry
from . import settings as settings_module
from .agent import base as agent
from .commands import (
    Ask,
    Command,
    Connect,
    Disconnect,
    Fetch,
    Find,
    ListSources,
)
from .connectors import base as connectors
from .models import (
    Capability,
    CapabilityError,
    ConnectError,
    Descriptor,
    Doc,
    Hit,
    HostReport,
    Message,
    Ref,
    RuntimeReport,
    RuntimeUnavailable,
    SessionError,
    SourceRequest,
    ToolSpec,
)


def _snake(name: str) -> str:
    """`RuntimeUnavailable` -> `runtime_unavailable`. The event vocabulary is snake_case
    everywhere else, and the recorded fixture already spelled it that way."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


NO_RUNTIME = "no agent runtime configured"
#: Kept separate from the message so a renderer can show what went wrong and what to do
#: as two lines. Folding them into one string printed the same sentence twice.
NO_RUNTIME_REMEDY = (
    "choose one with `/runtime claude-cli` in the shell, or set `runtime:` in "
    "~/.config/latent-intel/config.yaml. Run `intel doctor` to see which are available."
)


class Session:
    """Connected sources, and the operations over them."""

    def __init__(
        self,
        session_id: UUID | None = None,
        runtime: agent.Runtime | None = None,
        registry_path: str | None = None,
    ) -> None:
        self.session_id = session_id or uuid4()
        #: Which registry names resolve against. Injected rather than read from config,
        #: for the same reason `runtime` is: a Session must be constructible in a test
        #: without a config on disk.
        self._registry_path = registry_path
        #: Injected for tests and for a future `--runtime` flag; otherwise resolved from
        #: config on first `ask`.
        self._runtime = runtime
        self._runtime_kind: str | None = getattr(runtime, "id", None)
        self._history: list[Message] = []
        self._connectors: dict[str, connectors.Connector] = {}
        #: The source a bare key resolves against, set by `/use`.
        self.current: str | None = None
        #: Source id -> failure message, reset by each `find`. Read by `run` so a search
        #: that partly failed says which part, rather than looking like an empty corpus.
        self._failures: dict[str, str] = {}

    # -- typed API ------------------------------------------------------------

    async def connect(
        self,
        spec: str,
        *,
        kind: str | None = None,
        source_id: str | None = None,
        remote: bool = False,
        **options: Any,
    ) -> Descriptor:
        """Attach a source, by registered id or by path/URI.

        A registered name carries its own kind, so `connect("design")` is enough. A raw
        path does not, so the kind is required — guessing it from a directory's contents
        would be wrong exactly when it matters.
        """
        if kind is None:
            kind, target = registry.resolve(spec, self._registry_path, remote=remote)
            source_id = source_id or spec
        else:
            target = registry.expand(spec)
            source_id = source_id or _id_from(target)

        if source_id in self._connectors:
            raise ConnectError(
                f"'{source_id}' is already connected — disconnect it first, "
                f"or pass a different source_id"
            )

        connector = await connectors.open_connector(
            connectors.build(kind, source_id, target, **options)
        )
        self._connectors[source_id] = connector
        if self.current is None:
            self.current = source_id
        return connector.describe()

    async def connect_many(self, specs: Sequence[SourceRequest]) -> list[str]:
        """Attach several sources, in declared order.

        **Opening cannot run in a task group**, tempting as the fan-out is. A stdio MCP
        server's transport is an `@asynccontextmanager` that yields inside a task group
        of its own, so its cancel scope belongs to whichever task entered it and anyio
        requires that same task to exit it. `McpConnector.aopen` deliberately leaves it
        open — the subprocess has to outlive the handshake — and `aclose` unwinds it
        from the task that owns the session. Open it in a child task that then returns
        and the next scope exit raises `Attempted to exit a cancel scope that isn't the
        current task's`. `find` may fan out because it finishes inside the child and
        holds nothing afterwards; a connector outlives the call, so it may not.

        `sources()` documents connection order and `Ref.parse` resolves a bare key
        against `current`, so the order sources arrive in is observable behaviour
        either way.

        Returns the failures, one message per source, rather than raising: one
        unreachable source must not stop the others. That is the behaviour
        `open_session` had and this replaces.
        """
        opened: dict[int, Any] = {}
        problems: dict[int, str] = {}

        async def one(index: int, request: SourceRequest) -> None:
            try:
                if request.kind is None:
                    kind, target = registry.resolve(
                        request.spec, self._registry_path, remote=request.remote
                    )
                    source_id = request.source_id or request.spec
                else:
                    kind, target = request.kind, registry.expand(request.spec)
                    source_id = request.source_id or _id_from(target)
                opened[index] = (
                    source_id,
                    await connectors.open_connector(
                        connectors.build(kind, source_id, target, **request.options)
                    ),
                )
            except SessionError as exc:
                problems[index] = f"{request.source_id or request.spec}: {exc}"

        for index, request in enumerate(specs):
            await one(index, request)

        failures = [problems[i] for i in sorted(problems)]
        for index in sorted(opened):
            source_id, connector = opened[index]
            if source_id in self._connectors:
                failures.append(f"{source_id}: already connected")
                await connector.aclose()
                continue
            self._connectors[source_id] = connector
            if self.current is None:
                self.current = source_id
        return failures

    async def disconnect(self, source_id: str) -> None:
        connector = self._connectors.pop(source_id, None)
        if connector is None:
            raise SessionError(f"'{source_id}' is not connected")
        await connector.aclose()
        if self.current == source_id:
            self.current = next(iter(self._connectors), None)

    def sources(self) -> list[Descriptor]:
        """Connected sources, in connection order."""
        return [c.describe() for c in self._connectors.values()]

    def available(self) -> list[Descriptor]:
        """Every registered store, connected or not — what `connect` could reach.

        Here rather than in the frontend because a frontend may not import the
        registry: the contract confines them to this API, the event models and the
        renderer, so a web client stays an addition rather than a rewrite.
        """
        rows: list[Descriptor] = []
        for name, entry in sorted(registry.load(self._registry_path).items()):
            rows.append(
                Descriptor(
                    id=name,
                    kind=entry.kind or entry.registry_kind,
                    title=entry.name,
                    detail={
                        "domain": entry.domain,
                        "target": entry.target() or "",
                        "mirror": entry.mirror or "",
                        "connected": name in self._connectors,
                        "connectable": entry.connectable,
                    },
                )
            )
        return rows

    @staticmethod
    def connector_kinds() -> list[str]:
        """Which connector kinds are installed, for `intel doctor`.

        Exposed here for the same reason as `available`: a frontend may not import the
        connector package, so anything it needs to *show* about connectors comes through
        this API.
        """
        return sorted(connectors.available_kinds())

    @staticmethod
    def mcp_server_from(
        path: str, name: str | None = None
    ) -> tuple[str, str, dict[str, Any]]:
        """One server out of a `.mcp.json`, as `(name, target, options)`.

        Here for the same reason as `connector_kinds`: reading another host's server
        registration is the MCP connector's knowledge, and a frontend may not import a
        connector to reach it.
        """
        from .connectors.mcp import read_mcp_config

        return read_mcp_config(path, name)

    @staticmethod
    def runtime_status() -> dict[str, str | None]:
        """Installed runtime kinds, each mapped to None when usable or to why not.

        Here for the same reason as `connector_kinds`: a frontend may not import the
        agent package. The reason rather than a bare boolean because "cannot run here"
        is not a diagnosis when any one of four environment variables could be the
        missing one — and finding out which meant reading the runtime's source.

        Names of environment variables, never values: this string is printed by
        `intel doctor`, whose output has to stay safe to paste into a support thread.
        """
        return {
            name: report.reason for name, report in Session.runtime_reports().items()
        }

    @staticmethod
    def runtime_reports() -> dict[str, RuntimeReport]:
        """Installed runtime kinds, each with its verdict and who owns its loop.

        One build per kind answering both questions, for the reason `host_report`
        gives: a family read off a runtime built a moment after the verdict can
        describe a different object than the verdict does. `runtime_status` derives
        from this rather than building everything a second time.
        """
        return {name: Session._report(name) for name in agent.available_kinds()}

    @staticmethod
    def runtime_reason(kind: str) -> str | None:
        """Why one runtime cannot run here, or None when it can.

        The whole diagnosis for one kind, because almost everything that asks wants one:
        the shell reports the runtime it just set, and sweeping every installed runtime
        to print a line about one of them builds the others for nothing. `doctor` is the
        only caller that wants them all, and gets them through `runtime_status`, which
        is this function in a loop — so the verdict a frontend prints and the verdict
        the table prints cannot be arrived at two different ways.

        Built the way `_resolve_runtime` builds it, or this diagnoses a different object
        than the one that answers: a configured model, host or key would all be
        invisible and the verdict wrong with them. An unknown kind reads as `build`
        already phrases it, since that is the failure someone will meet next.

        Names of environment variables, never values: this string is printed by
        `intel doctor`, whose output has to stay safe to paste into a support thread.
        """
        return Session._report(kind).reason

    @staticmethod
    def _report(kind: str) -> RuntimeReport:
        """One build of one runtime, read for everything a report carries.

        Split out so `runtime_reason` and `runtime_reports` cannot arrive at a verdict
        two ways, and so grouping every installed runtime by family costs one build
        each rather than two.
        """
        try:
            runtime = agent.build(kind, **Session._runtime_options(kind))
        except Exception as exc:  # noqa: BLE001 — a broken runtime is not fatal
            return RuntimeReport(reason=str(exc) or "could not be built")
        return RuntimeReport(
            reason=Session._verdict(runtime), family=Session._family(runtime)
        )

    @staticmethod
    def _family(runtime: agent.Runtime) -> str:
        """Who owns one built runtime's loop, as it declares it.

        Declared, never inferred, and never guessed at either: a runtime that declares
        nothing, raises, or names a family this build does not know reads as the
        default. A frontend groups by these names, and inventing a group for a word we
        cannot place would put a runtime somewhere nobody looks.
        """
        if not isinstance(runtime, agent.Owned):
            return agent.DEFAULT_FAMILY
        try:
            family = runtime.family()
        except Exception:  # noqa: BLE001 — a broken runtime is not fatal
            return agent.DEFAULT_FAMILY
        return family if family in agent.FAMILIES else agent.DEFAULT_FAMILY

    @staticmethod
    def _verdict(runtime: agent.Runtime) -> str | None:
        """One built runtime's own verdict on itself, or None when it can run.

        Split out so `host_report` reaches the same verdict as `runtime_reason` without
        building the runtime a second time to get it. Two callers arriving at it two
        ways is how a table and a line about one of its rows come to disagree.
        """
        if runtime.available():
            return None
        reason = (
            runtime.unavailable_reason()
            if isinstance(runtime, agent.Diagnosable)
            else None
        )
        return reason or "installed, but cannot run here"

    @staticmethod
    def host_report(kind: str) -> HostReport:
        """Every endpoint one runtime declares, and what each still needs.

        The question `runtime_reason` cannot answer: it diagnoses the host that is
        configured, which tells someone why today failed but not which of the others
        they could reach instead, or what the one they are about to choose would cost
        them. Both readings come off the same `Host` rows, so a host reported ready
        here is ready in exactly the sense `doctor` means when it is the configured one.

        One build, three answers. Asking `runtime_reason`, `runtime_setting` and the
        host table separately would build the runtime three times and let the verdict
        describe a different object than the host list beside it.

        A runtime that declares no hosts — `claude-cli`, which shells to a binary that
        has already chosen — reports an empty table rather than an error. It has
        nothing to say here, which is not the same as something being wrong.
        """
        try:
            runtime = agent.build(kind, **Session._runtime_options(kind))
        except Exception as exc:  # noqa: BLE001 — a broken runtime is not fatal
            return HostReport(runtime=kind, reason=str(exc) or "could not be built")
        return HostReport(
            runtime=kind,
            configured=Session._setting(runtime, "host"),
            reason=Session._verdict(runtime),
            hosts=runtime.host_status() if isinstance(runtime, agent.Hosted) else {},
        )

    @staticmethod
    def runtime_setting(kind: str, key: str) -> str | None:
        """One option as the runtime it was built with holds it, or None where it holds
        nothing.

        What a frontend reports, and deliberately not what the config file says: a
        runtime resolves its own host across the user's file, the project's `agent:`
        block and `LATENT_INTEL_<RUNTIME>_HOST`, and only the built object knows which
        of those won. Reading the file reported a value that is in force as unset, and
        reported `none` after clearing an override that a project still declares.

        None where the runtime cannot be built at all: the caller is reporting a
        setting, and the reason it could not be built is `runtime_reason`'s to give.
        """
        try:
            runtime = agent.build(kind, **Session._runtime_options(kind))
        except Exception:  # noqa: BLE001 — a broken runtime has no setting to report
            return None
        return Session._setting(runtime, key)

    @staticmethod
    def _setting(runtime: agent.Runtime, key: str) -> str | None:
        """One option off a runtime that is already built. See `runtime_setting` for
        why it is read off the object and not out of the file."""
        value = getattr(runtime, key, None)
        return str(value) if value else None

    @staticmethod
    def runtime_kinds() -> dict[str, bool]:
        """Installed runtime kinds, each mapped to whether it can run here.

        Derived from `runtime_status` so the two can never disagree, and kept because
        a caller that only wants the mark should not have to compare against None.
        """
        return {
            name: reason is None for name, reason in Session.runtime_status().items()
        }

    @property
    def runtime_kind(self) -> str | None:
        """Which runtime will answer, if one is chosen."""
        return self._runtime_kind

    def set_runtime(self, kind: str | None) -> None:
        """Choose a runtime by name, or None to clear.

        Validates now rather than at `ask` time, so `/runtime typo` fails where it was
        typed instead of at the next question.
        """
        if kind is None:
            self._runtime = None
            self._runtime_kind = None
            return
        self._runtime = agent.build(kind, **self._runtime_options(kind))
        self._runtime_kind = kind

    @staticmethod
    def _runtime_options(kind: str) -> dict[str, Any]:
        """What to build one runtime with — the resolved view, not the user's file.

        Both halves of this were silently dropped before. A project declaring
        `agent: {runtimes: {custom: {model: ...}}}` never reached the runtime it
        named, because only `config.load()` was read; and `approval:` was resolved by
        the settings layer and then consumed by nobody, so a user who chose `auto` got
        the cautious gate anyway. A setting that does nothing is worse than one that is
        missing: the file says it is on.
        """
        resolved = settings_module.load()
        # `approval` last: it is resolved across all three layers, and a copy nested
        # under `runtimes:<kind>:` would otherwise override the resolved one from the
        # layer beneath it. `settings.load()` reports that key rather than honouring it.
        return {**resolved.runtime_options(kind), "approval": resolved.approval}

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Connected sources a subprocess agent can reach, as an `mcpServers` block.

        Sources that exist only in this process are absent — see `connectors.Servable`.
        A caller showing this to a person should say so, rather than implying the agent
        sees everything that is attached.
        """
        servers: dict[str, dict[str, Any]] = {}
        for source_id, connector in self._connectors.items():
            if not isinstance(connector, connectors.Servable):
                continue
            spec = connector.server_spec()
            if spec:
                servers[source_id] = spec
        return servers

    def tools(self) -> list[ToolSpec]:
        """The router's namespace: every tool every connected source offers.

        Names are qualified with the source id, so two sources may both offer `search`
        without one shadowing the other.
        """
        specs: list[ToolSpec] = []
        for connector in self._connectors.values():
            if isinstance(connector, connectors.ToolProvider):
                specs.extend(connector.tools())
        return specs

    async def call_tool(
        self, source_id: str, name: str, arguments: dict[str, Any]
    ) -> str:
        """Run one tool on one connected source — the router an in-process runtime uses.

        Raises rather than returning an error string. A runtime turns the exception
        into a `ToolResult` with `ok=False`, which is a different thing on the wire
        from output; a string would reach the model looking like an answer.
        """
        connector = self._connectors.get(source_id)
        if connector is None:
            raise SessionError(f"'{source_id}' is not connected")
        if not isinstance(connector, connectors.ToolProvider):
            raise CapabilityError(
                f"'{source_id}' is a {connector.kind} source and offers no tools"
            )
        return await connector.call(name, arguments)

    async def find(
        self,
        query: str,
        *,
        source: str | None = None,
        limit: int = 10,
        **filters: Any,
    ) -> dict[str, list[Hit]]:
        """Search, grouped by source. Never a merged ranking — see the module docstring.

        Sources are searched concurrently, so the cost is the slowest source rather than
        their sum. A source that fails contributes an empty list rather than taking the
        whole search down with it; `run` surfaces the failure as an event.
        """
        targets = self._searchable(source)
        results: dict[str, list[Hit]] = {name: [] for name in targets}
        self._failures = {}

        async def one(name: str) -> None:
            connector = self._connectors[name]
            assert isinstance(connector, connectors.Searchable)
            try:
                results[name] = await connector.search(query, limit=limit, **filters)
            except Exception as exc:  # noqa: BLE001 — one bad source is not a bad search
                self._failures[name] = str(exc)

        async with anyio.create_task_group() as group:
            for name in targets:
                group.start_soon(one, name)
        return results

    async def fetch(self, ref: str) -> Doc:
        """One document, addressed `source:key` — or a bare key against `/use`."""
        parsed = Ref.parse(ref, default_source=self.current)
        connector = self._connectors.get(parsed.source_id)
        if connector is None:
            raise SessionError(f"'{parsed.source_id}' is not connected")
        if not isinstance(connector, connectors.Fetchable):
            raise CapabilityError(
                f"'{parsed.source_id}' is a {connector.kind} source and cannot fetch "
                f"documents"
            )
        return await connector.fetch(parsed.key)

    async def ask(self, prompt: str) -> AsyncIterator[ev.AgentEvent]:
        """One turn through the configured runtime.

        The runtime is resolved on first use rather than in `__init__`: a `Session` is
        built on every CLI invocation, and `intel search` should not pay for an
        entry-point scan and a config read it never uses.

        Persona and skills travel beside `sources` rather than as runtime options: they
        are the project's, not one runtime's, and resolving them here is what stops
        every runtime re-reading the project to find them.
        """
        runtime = self._resolve_runtime()
        resolved = settings_module.load()
        emitter = ev.Emitter(self.session_id)
        self._history.append(Message(role="user", text=prompt))
        async for event in runtime.stream(
            list(self._history),
            self.tools(),
            emitter=emitter,
            mcp_servers=self.mcp_servers(),
            sources=self.sources(),
            persona=resolved.persona,
            persona_mode=resolved.persona_mode,
            skills=resolved.skills,
            call_tool=self.call_tool,
        ):
            if isinstance(event, ev.AgentCompleted):
                self._history.append(Message(role="assistant", text=event.text))
            yield event

    def _resolve_runtime(self) -> agent.Runtime:
        """The configured runtime, or a failure naming what to do about it."""
        if self._runtime is not None:
            return self._runtime
        resolved = settings_module.load()
        if not resolved.runtime:
            raise RuntimeUnavailable(NO_RUNTIME)
        kind = resolved.runtime
        runtime = agent.build(kind, **self._runtime_options(kind))
        if not runtime.available():
            reason = (
                runtime.unavailable_reason()
                if isinstance(runtime, agent.Diagnosable)
                else None
            )
            raise RuntimeUnavailable(
                f"the '{kind}' runtime is configured but cannot run here"
                + (f" — {reason}" if reason else "")
            )
        self._runtime = runtime
        self._runtime_kind = kind
        return runtime

    async def aclose(self) -> None:
        for connector in list(self._connectors.values()):
            await connector.aclose()
        self._connectors.clear()
        self.current = None

    # -- event API ------------------------------------------------------------

    async def run(self, command: Command) -> AsyncIterator[ev.AgentEvent]:
        """Run one command, streaming envelope-stamped events.

        Every failure a person should see is emitted rather than raised: a frontend
        iterating this over a transport has nowhere to catch an exception.
        """
        emit = ev.Emitter(self.session_id)
        try:
            if isinstance(command, Connect):
                descriptor = await self.connect(
                    command.spec,
                    kind=command.kind,
                    source_id=command.source_id,
                    remote=command.remote,
                    **command.options,
                )
                yield emit.emit(ev.SourceConnected, descriptor=descriptor)
                if tools := self.tools():
                    yield emit.emit(ev.ToolsChanged, tools=tools)

            elif isinstance(command, Disconnect):
                await self.disconnect(command.source_id)
                yield emit.emit(ev.SourceDisconnected, source_id=command.source_id)
                yield emit.emit(ev.ToolsChanged, tools=self.tools())

            elif isinstance(command, ListSources):
                for descriptor in self.sources():
                    yield emit.emit(ev.SourceConnected, descriptor=descriptor)

            elif isinstance(command, Find):
                names = self._searchable(command.source)
                yield emit.emit(
                    ev.RetrievalStarted,
                    query=command.query,
                    sources=names,
                    filters={"limit": command.limit, **command.filters},
                )
                grouped = await self.find(
                    command.query,
                    source=command.source,
                    limit=command.limit,
                    **command.filters,
                )
                for name, hits in grouped.items():
                    yield emit.emit(
                        ev.RetrievalResult,
                        source_id=name,
                        query=command.query,
                        hits=hits,
                        truncated=len(hits) >= command.limit,
                    )
                for name, message in self._failures.items():
                    yield emit.emit(
                        ev.AgentFailed,
                        message=f"{name}: {message}",
                        kind="source_error",
                        remedy=f"`/disconnect {name}` to drop it from this search",
                    )

            elif isinstance(command, Fetch):
                yield emit.emit(ev.DocumentFetched, doc=await self.fetch(command.ref))

            elif isinstance(command, Ask):
                yield emit.emit(ev.UserMessage, text=command.prompt)
                async for event in self.ask(command.prompt):
                    yield event

        except SessionError as exc:
            yield emit.emit(
                ev.AgentFailed,
                message=str(exc),
                kind=_snake(type(exc).__name__),
                remedy=NO_RUNTIME_REMEDY if isinstance(exc, RuntimeUnavailable) else "",
            )

    # -- internals ------------------------------------------------------------

    def _searchable(self, source: str | None) -> list[str]:
        """Which connected sources a search should reach."""
        if source is not None:
            connector = self._connectors.get(source)
            if connector is None:
                raise SessionError(f"'{source}' is not connected")
            if not connector.describe().can(Capability.SEARCH):
                raise CapabilityError(f"'{source}' does not support search")
            return [source]
        return [
            name
            for name, connector in self._connectors.items()
            if connector.describe().can(Capability.SEARCH)
        ]


def _id_from(target: str) -> str:
    """A readable default id from a path or URI — the last meaningful segment."""
    cleaned = target.rstrip("/")
    tail = cleaned.rsplit("/", 1)[-1] if "/" in cleaned else cleaned
    return tail or "source"


__all__ = ["NO_RUNTIME", "Session"]
