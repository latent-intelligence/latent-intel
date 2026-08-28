"""The connector seam — where context comes from.

One of the two extension points, and deliberately small.

**Capabilities compose; they are not one fat Protocol.** An MCP server may expose only
tools. A directory of markdown has no tools. A wiki has all three. Requiring every
connector to implement `search`, `fetch` and `tools` would force empty methods that
raise or return nothing, and a caller could not tell a source that *has* no tools from
one whose tool call failed.

**A connector cannot claim what it does not implement.** `describe()` declares
capabilities and `verify_capabilities` checks each declaration against the object, at
connect time. A source that says it can search and then has no `search` fails loudly
when it is attached rather than silently when someone searches.

**Connectors return values, not events.** A connector author never touches an envelope,
a sequence number or an operation id — the session wraps results into events. This keeps
a third-party connector to a handful of ordinary async methods, and means the event
vocabulary can change without touching a single connector.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol, cast, runtime_checkable

from ..models import (
    Capability,
    ConnectError,
    Descriptor,
    Doc,
    Hit,
    ToolSpec,
)

#: The entry-point group a third party publishes into. Our own adapters use it too, so
#: the extension path is the one we take ourselves and cannot rot unnoticed.
ENTRY_POINT_GROUP = "latent_intel.connectors"


@runtime_checkable
class Connector(Protocol):
    """What every source is, regardless of what it can do."""

    id: str
    kind: str

    def describe(self) -> Descriptor:
        """What this is, cheaply — including which capabilities it offers."""
        ...

    async def aclose(self) -> None:
        """Release anything held. Called once, and safe to call twice."""
        ...


@runtime_checkable
class AsyncOpen(Protocol):
    """A connector whose setup is I/O and cannot happen in `__init__`.

    An MCP server is a subprocess or an HTTP request; a directory of markdown is not.
    Rather than make every connector async to construct, this is optional and the
    session awaits it when present — the same compose-what-you-need shape as the
    capabilities above.
    """

    async def aopen(self) -> None: ...


@runtime_checkable
class Servable(Protocol):
    """A source a *subprocess* agent can reach, as an MCP server launch spec.

    Not a `Capability`. It says nothing about what the source can do — only whether
    there is a way to hand it to a runtime that owns its own tool loop and therefore
    cannot call our in-process Python. A connector that only exists in this process
    (`files`, `vector`) simply does not implement it, and the agent is told which
    sources it can actually see rather than being handed a list with holes in it.
    """

    def server_spec(self) -> dict[str, Any] | None:
        """A `mcpServers` entry, or None when this source cannot be served."""
        ...


@runtime_checkable
class Searchable(Protocol):
    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        """Rank within this source only.

        The score returned is comparable *inside* this source and nowhere else. The
        session never merges hits from two sources into one ordered list, because a
        lexical count and a vector cosine are different quantities.
        """
        ...


@runtime_checkable
class Fetchable(Protocol):
    async def fetch(self, key: str) -> Doc:
        """One document in full — the detail rung. `key` is the part after the colon."""
        ...


@runtime_checkable
class ToolProvider(Protocol):
    def tools(self) -> list[ToolSpec]:
        """What this source contributes to the router's namespace."""
        ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one of them. `name` is unqualified — the router strips the prefix."""
        ...


#: Capability -> the Protocol that has to be satisfied to claim it.
CAPABILITY_PROTOCOLS: dict[Capability, type] = {
    Capability.SEARCH: Searchable,
    Capability.FETCH: Fetchable,
    Capability.TOOLS: ToolProvider,
}


def verify_capabilities(connector: Connector) -> None:
    """Refuse a connector whose declaration and implementation disagree.

    Checked when a source is attached rather than when it is used. The failure a person
    can act on is "this connector is broken"; the failure they cannot is a search that
    returns nothing and looks like an empty corpus.
    """
    declared = connector.describe().capabilities
    missing = [
        capability
        for capability in declared
        if not isinstance(connector, CAPABILITY_PROTOCOLS[capability])
    ]
    if missing:
        names = ", ".join(sorted(str(m) for m in missing))
        raise ConnectError(
            f"connector '{connector.id}' declares {names} but does not implement "
            f"the matching method — this is a bug in the connector, not in the store"
        )


def available_kinds() -> dict[str, type]:
    """Every connector class registered under the entry-point group.

    Loaded on demand rather than imported at module scope: a connector whose optional
    dependency is missing should be absent from the list, not an ImportError at
    start-up.
    """
    kinds: dict[str, type] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            kinds[entry.name] = entry.load()
        except Exception:  # noqa: BLE001 — a broken plugin must not stop the program
            continue
    return kinds


def build(kind: str, source_id: str, target: str, **options: Any) -> Connector:
    """Instantiate one connector by kind.

    Every adapter takes the same three arguments — an id, something to point at, and
    whatever else it needs — so adding a kind never changes this function.
    """
    kinds = available_kinds()
    if kind not in kinds:
        known = ", ".join(sorted(kinds)) or "none installed"
        raise ConnectError(
            f"no connector of kind '{kind}' (installed: {known}). "
            "Kinds arrive as entry points, so this one needs a package that "
            "registers it."
        )
    return cast(Connector, kinds[kind](source_id, target, **options))


async def open_connector(connector: Connector) -> Connector:
    """Finish setup, then check the declaration against the implementation.

    The order matters for MCP: a server's capabilities are not known until it has been
    started and asked, so verifying before `aopen` would check an empty list and pass
    everything.
    """
    if isinstance(connector, AsyncOpen):
        await connector.aopen()
    verify_capabilities(connector)
    return connector


__all__ = [
    "CAPABILITY_PROTOCOLS",
    "Servable",
    "ENTRY_POINT_GROUP",
    "AsyncOpen",
    "Connector",
    "Fetchable",
    "Searchable",
    "ToolProvider",
    "available_kinds",
    "build",
    "open_connector",
    "verify_capabilities",
]
