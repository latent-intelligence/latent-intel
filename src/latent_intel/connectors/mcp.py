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

**The four keys a server registration carries, and what each becomes here.** `command`
and `args` are the target, one string split with `shlex`. `env` is a list, each item a
bare `NAME` that forwards this process's value or a `NAME=value` literal — the SDK's
stdio transport starts a child with six variables and nothing else, so a server that
runs under another host does not start here without it. `cwd` is for a server that
looks for its own `.env` in the working directory, and for `python -m`. Both go
straight to `StdioServerParameters`; the SDK merges `env` over its own whitelist, so
merging again here would only disagree with it.

**Transport is the SDK's problem.** `Client` accepts a command string, a URL, or a
server object, and selects stdio or Streamable HTTP accordingly. Legacy SSE exists in
the SDK and is not offered here — it is compatibility, not a default worth choosing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from .. import registry
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

#: How much of a failed server's stderr to quote back. Enough for a traceback and its
#: message, short enough that a wall of warnings does not bury the command that failed.
_STDERR_TAIL = 2000

#: The `${NAME}` another host writes in its own config to mean "forward mine".
_ENV_REF = re.compile(r"\$\{(\w+)\}")

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
        #: What the child gets beyond the SDK's whitelist, and the subset of it that may
        #: be written down: a forwarded name's value belongs to this process only.
        self._env, self._env_literals = _env_of(source_id, options.pop("env", None))
        self._cwd = _cwd_of(source_id, options.pop("cwd", None))
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
        self._errlog: TextIO | None = None

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
        # A temp file rather than devnull, so a server that dies on startup can be
        # quoted back instead of discarded. On the stack, so it closes with everything
        # else — the devnull it replaces was opened and never closed at all.
        if not self.show_server_stderr:
            self._errlog = self._stack.enter_context(
                tempfile.TemporaryFile("w+")  # noqa: SIM115 — the stack is the manager
            )
        try:
            self._client = await self._stack.enter_async_context(
                Client(
                    _transport(
                        self.target,
                        env=self._env,
                        cwd=self._cwd,
                        errlog=self._errlog or sys.stderr,
                    ),
                    raise_exceptions=True,
                )
            )
            await self._discover()
        except ConnectError:
            await self.aclose()
            raise
        except Exception as exc:  # noqa: BLE001 — every transport failure lands here
            stderr = self._server_stderr()
            await self.aclose()
            message = f"{self.id}: could not start '{self.target}' — {_innermost(exc)}"
            raise ConnectError(f"{message}\n{stderr}" if stderr else message) from exc

    def _server_stderr(self) -> str:
        """What the server printed before it died, or nothing.

        Without it the whole report of a server that exits on a missing credential is
        `Connection closed` — the SDK's account of the pipe, not the server's own.
        """
        if self._errlog is None:
            return ""
        self._errlog.seek(0)
        return self._errlog.read()[-_STDERR_TAIL:].strip()

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
        self._errlog = None

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
        if not parts:
            return None
        spec: dict[str, Any] = {"command": parts[0], "args": parts[1:]}
        if self._cwd:
            spec["cwd"] = self._cwd
        # Literals only. This block reaches a subprocess agent on its command line, so a
        # forwarded value would show in `ps` — and that agent spawns its stdio servers
        # with its own environment, where a name forwarded from here already is.
        if self._env_literals:
            spec["env"] = dict(self._env_literals)
        return spec

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


def _transport(
    target: Any,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    errlog: TextIO | None = None,
) -> Any:
    """What to hand the SDK: a URL as-is, a command as a stdio transport.

    A URL goes straight through — `Client` picks Streamable HTTP for it. A command needs
    splitting into argv, and `stdio_client` satisfies the SDK's `Transport` protocol, so
    the two shapes meet at the same argument and no branch reaches further than this.
    Anything that is not a string is already something `Client` understands.

    `env` and `cwd` are passed through untouched: the SDK merges `env` over its own
    whitelist, and a second merge here could only disagree with it. The errlog belongs
    to `aopen`, which owns the file so it can read the server's stderr back afterwards.
    """
    if not isinstance(target, str):
        return target
    if target.startswith(("http://", "https://")):
        return target
    parts = shlex.split(target)
    if not parts:
        raise ConnectError("no server command given")

    from mcp.client.stdio import StdioServerParameters, stdio_client

    return stdio_client(
        StdioServerParameters(
            command=parts[0], args=parts[1:], env=env or None, cwd=cwd
        ),
        errlog=errlog or sys.stderr,
    )


def _innermost(exc: BaseException) -> BaseException:
    """The leaf of a nested ExceptionGroup, where the reason actually is.

    A stdio server that starts and exits immediately arrives as `Connection closed`
    wrapped in two groups, whose `str` is a count of sub-exceptions.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _env_of(source_id: str, value: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Read `env` into what the child gets, and the part of it that may be written down.

    A bare `NAME` forwards this process's value — names travel between machines and
    values do not, and `.env` is already applied by the time a source is attached. A
    `NAME=value` item is a literal, for the things that are not secrets: a region, a
    data path. A bare name that is unset is refused rather than dropped, because the
    server would otherwise start without it and fail somewhere far less readable.
    """
    if value is None:
        return {}, {}
    items = value.split(",") if isinstance(value, str) else list(value)
    env: dict[str, str] = {}
    literals: dict[str, str] = {}
    for item in items:
        name, assigned, literal = str(item).strip().partition("=")
        name = name.strip()
        if not name:
            continue
        if assigned:
            env[name] = literals[name] = literal
        elif name in os.environ:
            env[name] = os.environ[name]
        else:
            raise ConnectError(
                f"{source_id}: env {name} is not set — export it or put it in .env"
            )
    return env, literals


def _cwd_of(source_id: str, value: Any) -> str | None:
    """The directory a stdio server is started in, checked before it is used.

    Expanded like any other location (`~`, `${VAR}`), and a directory that does not
    exist is named here rather than arriving as a failed start with no reason.
    """
    if value is None:
        return None
    path = registry.expand(str(value))
    if not os.path.isdir(path):
        raise ConnectError(f"{source_id}: cwd {path} is not a directory")
    return path


def read_mcp_config(
    path: str | Path, name: str | None = None
) -> tuple[str, str, dict[str, Any]]:
    """One server out of a `.mcp.json` or `.vscode/mcp.json`.

    Returns `(name, target, options)`. A server already registered with another host is
    attached without retyping it, and is recorded as an ordinary source row — so the
    JSON file is read once, at `connect`, and never depended on again.
    """
    file = Path(path).expanduser()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectError(f"could not read {file} — {exc}") from exc

    # VS Code's key is `servers`; Claude Code's is `mcpServers`. Both are read because
    # both files are things a teammate already has.
    entries = raw if isinstance(raw, dict) else {}
    servers = entries.get("mcpServers") or entries.get("servers")
    if not isinstance(servers, dict) or not servers:
        raise ConnectError(f"{file}: no `mcpServers` and no `servers` mapping")

    known = ", ".join(sorted(servers))
    if name is None and len(servers) > 1:
        raise ConnectError(f"{file} holds several servers — name one of: {known}")
    chosen = name or next(iter(servers))
    entry = servers.get(chosen)
    if not isinstance(entry, dict):
        raise ConnectError(f"{file} has no server '{chosen}' — it holds: {known}")
    if "headers" in entry:
        raise ConnectError(f"{chosen}: headers are not supported")

    if url := entry.get("url"):
        target = str(url)
    elif command := entry.get("command"):
        target = shlex.join([str(command), *(str(a) for a in entry.get("args") or [])])
    else:
        raise ConnectError(f"{chosen}: names neither `command` nor `url`")

    options: dict[str, Any] = {}
    if env := _env_rows(chosen, entry.get("env")):
        options["env"] = env
    if cwd := entry.get("cwd"):
        # Resolved against the JSON file, which is the only base there is: by the time
        # the recorded row is read, nobody remembers where the file was. `~` is kept as
        # typed, for the same reason a source records what it was named.
        text = str(cwd)
        relative = not text.startswith("~") and not Path(text).is_absolute()
        options["cwd"] = str((file.parent / text).resolve()) if relative else text
    return chosen, target, options


def _env_rows(source_id: str, env: Any) -> list[str]:
    """Another host's `env` mapping as our list of names and literals.

    `${NAME}` is that host's own expansion syntax and means "forward mine", which is
    exactly what a bare name is here. A plain value is already written down in the file
    the user wrote, so keeping it verbatim exposes nothing new.
    """
    rows: list[str] = []
    for key, value in (env or {}).items():
        text = str(value)
        reference = _ENV_REF.fullmatch(text)
        if reference is not None and reference.group(1) == str(key):
            rows.append(str(key))
        elif "${" in text:
            # Anything else in that syntax belongs to the host that wrote the file: a
            # `${OTHER}` would have to be forwarded under a name it does not have, and
            # a `${input:…}` is a prompt we never saw. Passing the text through would
            # hand the server a literal `${…}` and call it configured.
            raise ConnectError(
                f"{source_id}: env {key} is set from {text}, which only the host that "
                "wrote this file can expand — give it here as NAME or NAME=value"
            )
        else:
            rows.append(f"{key}={text}")
    return rows


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
