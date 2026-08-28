"""Any MCP server — the connector that makes this program a host.

This is the one connector for things we do not own: a GitHub server, Context7, a
client's internal server. Our own stores are reached directly instead (`wiki.py` imports
`latent_wiki`), because a subprocess and a tool-schema round trip buy nothing when the
code is already importable, and typed `Hit` objects beat text blobs.

**Capabilities come from the server's declared capabilities, never from tool names.**
A server advertising `tools` is a `ToolProvider`; one advertising `resources` is also
`Searchable` and `Fetchable`, because `resources/list` and `resources/read` are exactly
those two operations. Reading a capability off a tool *named* `search` would be the
same guessing this package refuses to do for effects.

**Effects are read from tool annotations, and absence is not permission.**
`readOnlyHint` and `destructiveHint` are optional in the protocol, so most servers
declare nothing. A tool with no annotation becomes `EXTERNAL_WRITE` with
`effect_declared=False`, so approval gates it and `intel doctor` lists it as
under-declared. That is the safe end of the guess, and it is recorded as a guess.

**Point at the server, not at a wrapper.** `stdio_client` terminates the process it
started. Give it `uv run … lw serve` and it kills `uv`, leaving the actual server as an
orphan holding whatever file descriptors it inherited — measured, and it survives the
client exiting. Give it the executable itself (`/path/.venv/bin/lw serve --store …`) and
cleanup is exact. `lw serve --print-config` emits the direct form for this reason.

**Transport is the SDK's problem.** `Client` accepts a command string, a URL, or a
server object, and selects stdio or Streamable HTTP accordingly. Legacy SSE exists in
the SDK and is not offered here — it is compatibility, not a default worth choosing.
"""

from __future__ import annotations

import os
import shlex
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from ..models import (
    Capability,
    ConnectError,
    Descriptor,
    Doc,
    Effect,
    Hit,
    Provenance,
    ToolSpec,
)

if TYPE_CHECKING:  # pragma: no cover
    from mcp import Client

#: A resource listing is one round trip; caching it makes `search` local after the first
#: call. Servers that change their resource set mid-session are rare, and `/disconnect`
#: then `/connect` is the honest way to pick that up.
_LIST_LIMIT = 500


class McpConnector:
    """One MCP server, connected for the life of the session."""

    kind = "mcp"

    def __init__(self, source_id: str, target: Any, **options: Any) -> None:
        self.id = source_id
        #: Where a stdio server's stderr goes. Discarded by default: it is the
        #: server's own diagnostics and it interleaves unreadably with rendered output.
        #: Pass `server_stderr=True` to watch it while debugging a server.
        self.show_server_stderr = bool(options.pop("server_stderr", False))
        #: A command line or a URL from the CLI. A Python caller may also pass an MCP
        #: server object, which `Client` accepts directly — that runs it in-process with
        #: no subprocess, which is how the tests here work and how an embedded server
        #: would.
        self.target = target if not isinstance(target, str) else str(target)
        self.options = options
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[ToolSpec] = []
        self._resources: list[tuple[str, str, str]] = []
        self._capabilities: list[Capability] = []

    # -- lifecycle ------------------------------------------------------------

    async def aopen(self) -> None:
        """Start the server and learn what it offers.

        Separate from `__init__` because connecting is I/O: a stdio server is a
        subprocess and an HTTP one is a request, and neither belongs in a constructor
        that `build()` calls synchronously.
        """
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover — mcp is a base dependency
            raise ConnectError("the mcp package is not installed") from exc

        self._stack = AsyncExitStack()
        try:
            self._client = await self._stack.enter_async_context(
                Client(
                    _transport(self.target, self.show_server_stderr),
                    raise_exceptions=True,
                )
            )
            await self._discover()
        except ConnectError:
            await self.aclose()
            raise
        except Exception as exc:  # noqa: BLE001 — every transport failure lands here
            await self.aclose()
            raise ConnectError(
                f"{self.id}: could not start '{self.target}' — {exc}"
            ) from exc

    async def _discover(self) -> None:
        """One round trip per capability, at connect time rather than per query."""
        assert self._client is not None
        declared = self._client.server_capabilities

        if declared is not None and declared.tools is not None:
            listed = await self._client.list_tools()
            self._tools = [_tool_spec(self.id, tool) for tool in listed.tools]
            self._capabilities.append(Capability.TOOLS)

        if declared is not None and declared.resources is not None:
            listed_resources = await self._client.list_resources()
            self._resources = [
                (str(r.uri), r.name or str(r.uri), r.description or "")
                for r in listed_resources.resources[:_LIST_LIMIT]
            ]
            # Declaring the capability is not the same as having any. `lw serve`
            # advertises `resources` and registers none, and claiming SEARCH there
            # would put an always-empty heading in every result set.
            if self._resources:
                self._capabilities.extend([Capability.SEARCH, Capability.FETCH])

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    # -- the contract ---------------------------------------------------------

    def describe(self) -> Descriptor:
        return Descriptor(
            id=self.id,
            kind=self.kind,
            title=str(self.target),
            capabilities=list(self._capabilities),
            count=len(self._tools) + len(self._resources) or None,
            unit="items",
            detail={
                "server": str(self.target),
                "tools": len(self._tools),
                "resources": len(self._resources),
                "undeclared_effects": sum(
                    1 for t in self._tools if not t.effect_declared
                ),
            },
        )

    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        """Substring match over the resource listing.

        Deliberately shallow: MCP gives a name and a description per resource, not a
        body, so there is nothing deeper to rank on without reading every resource —
        which would be one request each and is what `fetch` is for.
        """
        needle = query.lower().strip()
        if not needle or not self._resources:
            return []
        hits: list[Hit] = []
        for uri, name, description in self._resources:
            haystack = f"{uri} {name} {description}".lower()
            count = haystack.count(needle)
            if not count:
                continue
            hits.append(
                Hit(
                    ref=f"{self.id}:{uri}",
                    source_id=self.id,
                    title=name,
                    kind="resource",
                    lead=description[:280],
                    score=float(count + 5 * (needle in name.lower())),
                    provenance=Provenance(
                        source_id=self.id,
                        method="search",
                        ref=f"{self.id}:{uri}",
                        origin=uri,
                    ),
                )
            )
        hits.sort(key=lambda h: (-h.score, h.ref))
        return hits[:limit]

    async def fetch(self, key: str) -> Doc:
        """Read one resource by URI."""
        if self._client is None:
            raise ConnectError(f"{self.id} is not connected")
        try:
            result = await self._client.read_resource(key)
        except Exception as exc:  # noqa: BLE001 — protocol errors are ordinary failures
            raise ConnectError(f"{self.id}: could not read '{key}' — {exc}") from exc

        title = next((n for u, n, _ in self._resources if u == key), key)
        return Doc(
            ref=f"{self.id}:{key}",
            source_id=self.id,
            title=title,
            body=_text_of(result.contents),
            kind="resource",
            metadata={"uri": key, "server": str(self.target)},
            provenance=Provenance(
                source_id=self.id, method="fetch", ref=f"{self.id}:{key}", origin=key
            ),
        )

    def server_spec(self) -> dict[str, Any] | None:
        """How a subprocess agent would launch this same server.

        None when the target is an in-process server object: there is no command line
        that reproduces it, and inventing one would point the agent at something else.
        """
        if not isinstance(self.target, str):
            return None
        if self.target.startswith(("http://", "https://")):
            return {"type": "http", "url": self.target}
        parts = shlex.split(self.target)
        return {"command": parts[0], "args": parts[1:]} if parts else None

    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._client is None:
            raise ConnectError(f"{self.id} is not connected")
        try:
            result = await self._client.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 — transport and protocol faults alike
            raise ConnectError(f"{self.id}: {name} failed — {exc}") from exc

        # A tool that fails is a *result*, not an exception — `is_error` is part of the
        # protocol, and an agent has to see the text to retry or choose differently.
        # Only a transport fault above is exceptional. (The field is snake_case in SDK
        # v2; the camelCase spelling silently never matched.)
        if getattr(result, "is_error", False):
            return f"error: {_text_of(result.content)}"
        return _text_of(result.content)


def _transport(target: Any, show_stderr: bool = False) -> Any:
    """What to hand the SDK: a URL as-is, a command as a stdio transport.

    A URL goes straight through — `Client` picks Streamable HTTP for it. A command needs
    splitting into argv, and `stdio_client` satisfies the SDK's `Transport` protocol, so
    the two shapes meet at the same argument and no branch reaches further than this.
    Anything that is not a string is already something `Client` understands.
    """
    if not isinstance(target, str):
        return target
    if target.startswith(("http://", "https://")):
        return target
    parts = shlex.split(target)
    if not parts:
        raise ConnectError("no server command given")

    import sys

    from mcp.client.stdio import StdioServerParameters, stdio_client

    errlog = sys.stderr if show_stderr else open(os.devnull, "w")  # noqa: SIM115
    return stdio_client(
        StdioServerParameters(command=parts[0], args=parts[1:]), errlog=errlog
    )


def _tool_spec(source_id: str, tool: Any) -> ToolSpec:
    """Read a tool's declared effect, and record whether it declared one at all."""
    annotations = getattr(tool, "annotations", None)
    effect, declared = _effect_of(annotations)
    return ToolSpec(
        name=tool.name,
        source_id=source_id,
        description=(getattr(tool, "description", "") or "").strip(),
        effect=effect,
        effect_declared=declared,
        input_schema=getattr(tool, "inputSchema", None) or {},
    )


def _effect_of(annotations: Any) -> tuple[Effect, bool]:
    """Map MCP hints onto our effect vocabulary.

    Returns `(effect, declared)`. The second value is what keeps this honest: an
    unannotated tool gets the cautious answer *and* says that the answer was assumed,
    so `intel doctor` can list it rather than silently treating a guess as a fact.
    """
    if annotations is None:
        return Effect.EXTERNAL_WRITE, False

    destructive = getattr(annotations, "destructive_hint", None)
    read_only = getattr(annotations, "read_only_hint", None)
    open_world = getattr(annotations, "open_world_hint", None)

    if destructive is True:
        return Effect.DESTRUCTIVE, True
    if read_only is True:
        # A read that leaves the machine is still a read, but it is not `none` — it can
        # leak a query to a third party, which is a thing an approval policy may care
        # about.
        return (Effect.NONE if open_world is False else Effect.EXTERNAL_READ), True
    if read_only is False:
        return Effect.EXTERNAL_WRITE, True
    return Effect.EXTERNAL_WRITE, False


def _text_of(blocks: Any) -> str:
    """Flatten MCP content blocks to text, naming what could not be flattened."""
    parts: list[str] = []
    for block in blocks or []:
        if (text := getattr(block, "text", None)) is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content]")
    return "\n".join(parts)
