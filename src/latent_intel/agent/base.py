"""The runtime seam — where an answer comes from.

The second of the two extension points, and the mirror of `connectors/base.py`. That
parallel is deliberate: a runtime is discovered, built and reported exactly the way a
connector is, so someone who has read one file already knows this one.

**A runtime owns how much of the loop it wants.** `custom` hands the tool list to
our own router and drives the turn itself. `claude-cli` shells to a binary with its own
agent loop, reaching our sources as MCP servers instead. Both asymmetries live behind
`stream`; what comes out is the same event stream either way.

**`stream` is declared `def`, not `async def`, and that is not a slip.** An `async def`
whose body yields has type `Callable[..., AsyncIterator[T]]`, but a Protocol member
*written* `async def ... -> AsyncIterator[T]` types as
`Callable[..., Coroutine[Any, Any, AsyncIterator[T]]]`. Under `mypy --strict` those do
not match, and no implementation would satisfy the Protocol.

**A runtime never stamps an envelope.** The session builds the `Emitter` and passes it
in, so sequence numbers and operation ids stay out of a runtime author's hands — the
same bargain connectors get by returning values instead of events.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from importlib.metadata import entry_points
from typing import Any, Protocol, cast, runtime_checkable

from .. import events as ev
from ..models import (
    WEB_MODES,
    HostStatus,
    Message,
    RuntimeUnavailable,
    ToolSpec,
    WebScope,
)

#: The entry-point group a third party publishes into. Our own runtimes use it too, so
#: the extension path is the one we take ourselves and cannot rot unnoticed.
ENTRY_POINT_GROUP = "latent_intel.runtimes"


@runtime_checkable
class Runtime(Protocol):
    """One way of getting a model to answer, behind one event stream."""

    id: str

    def available(self) -> bool:
        """Whether this backend can actually run here — a binary on PATH, a key in the
        environment.

        Cheap and synchronous: `intel doctor` calls it for every installed runtime, and
        a probe that made a network request would make diagnosis slower than the thing
        being diagnosed.
        """
        ...

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        """One turn, as events. See the module docstring for why this is not `async`."""
        ...


@runtime_checkable
class Diagnosable(Protocol):
    """A runtime that can say *why* it cannot run, not merely that it cannot.

    Optional, and composed the way a connector composes `Servable` — an existing
    runtime need not implement it. `claude-cli` has one reason and its name is the
    remedy; a runtime reading four environment variables has four, and "cannot run
    here" sends someone to read source code to find out which.
    """

    def unavailable_reason(self) -> str | None:
        """Why this backend cannot run here, or None when it can.

        Names of environment variables, never their values: `intel doctor` prints this,
        and its output has to stay safe to paste into a support thread.
        """
        ...


@runtime_checkable
class Hosted(Protocol):
    """A runtime that reaches its model through one of several declared endpoints.

    Optional, and composed the way `Diagnosable` is: `claude-cli` shells to a binary
    that has already chosen its endpoint, and has nothing to declare. A runtime that
    does have a table can be asked about all of it at once rather than only about the
    row that happens to be configured, which is the difference between diagnosing a
    failure and planning a deployment.
    """

    def host_status(self) -> dict[str, HostStatus]:
        """Every host this runtime declares, each with what it still needs and the
        variables it is reading.

        Names of environment variables, never their values, for the same reason
        `unavailable_reason` gives: this is printed and pasted into support threads.
        """
        ...


#: Who owns the agent loop, as the four answers there are. `custom` is ours — the loop
#: in `runtimes/custom.py` over `agent/turn.py`. `sdk` is a vendor's runner driven in
#: this process, with tool execution still routed through us. `delegated` is another
#: harness on this machine, which owns orchestration, tools and permissions alike.
#: `managed` is a loop running on someone else's infrastructure.
FAMILIES: tuple[str, ...] = ("custom", "sdk", "delegated", "managed")

#: What a runtime that declares nothing is. Every runtime written before this Protocol
#: existed owns its own loop, so the default is the one that keeps them all correct.
DEFAULT_FAMILY = "custom"


@runtime_checkable
class Owned(Protocol):
    """A runtime that says who runs its agent loop.

    Optional, and composed the way `Diagnosable` and `Hosted` are: a runtime declaring
    nothing is `custom`, which is what every runtime written before this Protocol
    existed is. **Declared, never inferred** — the same rule tool effects follow.
    Reading it off `Hosted` would be coincidence: a delegated harness could perfectly
    well have a host table, and the first one that does would be silently misfiled.

    It matters because the flat list `doctor` printed blends two different things. A
    `custom` runtime's orchestration is ours to configure; a `delegated` one's is not,
    which is why `claude-cli` has no host rows and would take no `loop:`.
    """

    def family(self) -> str:
        """One of `FAMILIES`. A value outside it reads as `DEFAULT_FAMILY` — the caller
        reports what it was told and never guesses at a vocabulary it does not know."""
        ...


@runtime_checkable
class Delegating(Protocol):
    """A runtime that runs a project's subagents — see `subagents.py`.

    Optional, and composed the way `Hosted` is: implementing it is the declaration, and
    a runtime that does not is reported by `doctor` when a project declares delegates,
    rather than left to ignore them. One method rather than a flag beside another,
    because the question `doctor` has to answer is not only *whether* but *with what*.
    """

    def subagent_tools(self, tools: Sequence[ToolSpec]) -> frozenset[str]:
        """Every name a subagent's `tools:` may list on this runtime, given the tools
        attached: ours as `source.tool`, where this runtime would offer them, and any
        of its own it supplies.

        An implementation may also take `web: WebScope | None = None`, the scope in
        force, and is then passed it — see `subagent_tools` below. Optional so that a
        runtime written to the one-argument form keeps working.
        """
        ...


def subagent_tools(
    runtime: Delegating, tools: Sequence[ToolSpec], web: WebScope
) -> frozenset[str]:
    """What `runtime` would give a subagent, passing `web` only where the method takes
    it — by name or through `**kwargs`. A third-party runtime written before the scope
    existed would otherwise fail on an argument it never asked for."""
    method: Callable[..., Iterable[str]] = runtime.subagent_tools
    parameters = inspect.signature(method).parameters.values()
    if any(p.name == "web" or p.kind is p.VAR_KEYWORD for p in parameters):
        return frozenset(method(tools, web=web))
    return frozenset(method(tools))


#: The source id a web tool call is reported under, so `web:<url>` reads like any other
#: ref. Reserved by the session: a source attached under it would be indistinguishable.
WEB_ID = "web"


@runtime_checkable
class WebScoped(Protocol):
    """A runtime that can reach the open web, and says under which scopes.

    Optional, and composed the way `Hosted` is. A runtime that declares nothing honours
    `off` only — never inferred from a tool list or a host, because a scope a runtime
    silently ignored is an "internal-only" answer that read the web, or the reverse.
    Asked per scope rather than for a list, because the answer can turn on more than
    the mode: a host with no web tools, a domain list the runtime has no knob for.
    """

    def web_reason(self, scope: WebScope) -> str | None:
        """Why this runtime cannot honour `scope` as built, or None when it can.

        Asked through `web_reason` below, which answers `off` and a mode this build
        does not know before asking — so an implementation reads only what is its own.
        """
        ...


#: What to do about a refused scope, the same whichever runtime refused it. The reason
#: beside it is the runtime's own and names the specific way out.
WEB_REMEDY = (
    "`/scope off` (or `--web off`) to answer without the web, `/scope <mode> --any` "
    "to drop a domain list and max_uses, or a runtime that honours it — "
    "`intel doctor` lists which do"
)


def web_reason(runtime: Runtime, scope: WebScope) -> str | None:
    """Why `runtime` cannot honour `scope`, asked of it where it declares an answer.

    The one place the default lives, so the session's check, `doctor`'s listing and a
    runtime refusing its own turn cannot disagree — about `off`, about a mode this
    build does not know, or about a runtime that declares nothing.
    """
    if scope.mode == "off":
        return None
    if scope.mode not in WEB_MODES:
        return f"no web mode '{scope.mode}' — one of {', '.join(WEB_MODES)}"
    if isinstance(runtime, WebScoped):
        return runtime.web_reason(scope)
    return (
        f"the '{runtime.id}' runtime does not declare web access, so it honours only "
        f"`web: off`"
    )


def refuse_web(
    runtime: Runtime, scope: WebScope, emitter: ev.Emitter
) -> ev.AgentFailed | None:
    """The `AgentFailed` a turn ends on, before any request, when `runtime` cannot
    honour `scope` — or None when it can.

    Shared by the session and every runtime that checks for itself: a runtime is usable
    on its own, and a scope it cannot honour must not be quietly dropped.
    """
    reason = web_reason(runtime, scope)
    if reason is None:
        return None
    failed: ev.AgentFailed = emitter.emit(
        ev.AgentFailed, message=reason, kind="web_scope", remedy=WEB_REMEDY
    )
    return failed


def available_kinds() -> dict[str, type]:
    """Every runtime class registered under the entry-point group.

    Loaded on demand and failures swallowed, so a runtime whose optional dependency is
    missing — or whose plugin is broken — is simply absent from the list rather than an
    ImportError at start-up.
    """
    kinds: dict[str, type] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            kinds[entry.name] = entry.load()
        except Exception:  # noqa: BLE001 — a broken plugin must not stop the program
            continue
    return kinds


def build(kind: str, **options: Any) -> Runtime:
    """Instantiate one runtime by kind.

    Unlike `connectors.build` there is no id or target to pass: a runtime is not
    attached to anything, so forcing the parallel would mean two ignored arguments.
    """
    kinds = available_kinds()
    if kind not in kinds:
        known = ", ".join(sorted(kinds)) or "none installed"
        raise RuntimeUnavailable(f"no runtime of kind '{kind}' (installed: {known})")
    return cast(Runtime, kinds[kind](**options))


__all__ = [
    "DEFAULT_FAMILY",
    "ENTRY_POINT_GROUP",
    "FAMILIES",
    "WEB_ID",
    "WEB_REMEDY",
    "Delegating",
    "Diagnosable",
    "Hosted",
    "Owned",
    "Runtime",
    "WebScoped",
    "available_kinds",
    "build",
    "refuse_web",
    "subagent_tools",
    "web_reason",
]
