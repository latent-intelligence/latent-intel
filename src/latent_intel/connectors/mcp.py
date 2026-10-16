"""Any MCP server — the connector that makes this program a host.

This is how latent-intel reaches any MCP server: a GitHub server, Context7, a client's
internal server — and our own stores, through their adapters (`lw serve`,
`latent-records serve`). latent-intel reads no store's format itself; a store owns its
capability, and this client connects to it like any other.

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

**A wrapper is fine.** `stdio_client` in mcp 2.x terminates the whole process tree it
started, so `uv run --directory <checkout> <server> …` stops cleanly with the client —
measured 2026-10-12 against `lw serve`. (Under 1.x it killed only `uv` and orphaned the
server, which is why older notes say to point at the executable.)

**The four keys a server registration carries, and what each becomes here.** `command`
and `args` are the target, one string split with `shlex`. `env` is a list, each item a
bare `NAME` that forwards this process's value or a `NAME=value` literal — the SDK's
stdio transport starts a child with six variables and nothing else, so a server that
runs under another host does not start here without it. `cwd` is for a server that
looks for its own `.env` in the working directory, and for `python -m`. Both go
straight to `StdioServerParameters`; the SDK merges `env` over its own whitelist, so
merging again here would only disagree with it.

**Each connection lives in a task of its own.** anyio requires a cancel scope to be
exited by the task that entered it, in reverse order of entry, and a stdio transport is
one. Entered on the session's task, every server's scope sat on one stack, so closing
any but the newest raised. `aopen` therefore starts an asyncio task (the owner) that
enters the `Client`, signals ready and waits to be told to stop; `aclose` tells it and
waits, for `CLOSE_TIMEOUT` and then once more after cancelling it. Calls are made from
the caller's task against the owner's `Client`, so sources open and close in any order.
anyio has no task that outlives its caller, so the owner is asyncio's, and an MCP source
needs the asyncio event loop: under another, `aopen` refuses before anything starts.

**Transport is the SDK's problem.** `Client` accepts a command string, a URL, or a
server object, and selects stdio or Streamable HTTP accordingly. Legacy SSE exists in
the SDK and is not offered here — it is compatibility, not a default worth choosing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

import anyio

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

logger = logging.getLogger(__name__)

#: Owner tasks that outlived `aclose`, held so they are not collected mid-cleanup.
_abandoned: set[asyncio.Task[None]] = set()

#: How much of a failed server's stderr to quote back. Enough for a traceback and its
#: message, short enough that a wall of warnings does not bury the command that failed.
_STDERR_TAIL = 2000

#: The `${NAME}` another host writes in its own config to mean "forward mine".
_ENV_REF = re.compile(r"\$\{(\w+)\}")

#: A resource listing is one round trip; caching it makes `search` local after the first
#: call. Servers that change their resource set mid-session are rare, and `/disconnect`
#: then `/connect` is the honest way to pick that up.
_LIST_LIMIT = 500


#: How much of a server's own instructions this client keeps. They reach the system
#: prompt, and a third-party server is not the author of that prompt: a sentence or two
#: says what it holds, and anything longer is cut.
INSTRUCTIONS_MAX = 500

#: How long `aclose` waits for a server to stop, and then again for its task to end
#: once cancelled. The SDK gives a stdio server two seconds before it terminates it.
CLOSE_TIMEOUT = 5.0


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
        if options:
            # Nothing else is read here, and an option that configures nothing would
            # leave a source believing it was configured (a wiki's context, say, which
            # is the server's own --deployment or --context).
            raise ConnectError(
                f"{source_id}: an MCP source takes env and cwd; "
                f"{', '.join(sorted(options))} would configure nothing"
            )
        self.options = options
        self._owner: asyncio.Task[None] | None = None
        self._stop: anyio.Event | None = None
        self._failure: ConnectError | None = None
        self._client: Client | None = None
        self._tools: list[ToolSpec] = []
        self._resources: list[tuple[str, str, str]] = []
        self._capabilities: list[Capability] = []
        self._instructions = ""
        self._server_title = ""
        self._server_name = ""
        self._errlog: TextIO | None = None

    # -- lifecycle ------------------------------------------------------------

    async def aopen(self) -> None:
        """Start the server and learn what it offers.

        Separate from `__init__` because connecting is I/O: a stdio server is a
        subprocess and an HTTP one is a request, and neither belongs in a constructor
        that `build()` calls synchronously. The work happens in the owner task; this
        waits for it to be ready or to fail, and a caller cancelled meanwhile takes the
        owner down with it.
        """
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover — mcp is a base dependency
            raise ConnectError("the mcp package is not installed") from exc

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise ConnectError(
                f"{self.id}: latent-intel's MCP sources need the asyncio event loop; "
                "this one is running under another"
            ) from None
        ready = anyio.Event()
        self._stop = anyio.Event()
        self._failure = None
        self._owner = loop.create_task(self._own(Client, ready))
        try:
            await ready.wait()
        except BaseException:
            self._owner.cancel()
            await self.aclose()
            raise
        if self._failure is not None:
            failure, self._failure = self._failure, None
            await self.aclose()
            raise failure

    async def _own(self, client_type: type[Client], ready: anyio.Event) -> None:
        """The connection's whole life, entered and exited in this one task."""
        assert self._stop is not None
        try:
            with ExitStack() as files:
                try:
                    # A temp file rather than devnull, so a server that dies on
                    # startup can be quoted back instead of discarded.
                    if not self.show_server_stderr:
                        self._errlog = files.enter_context(
                            tempfile.TemporaryFile("w+")  # noqa: SIM115 — the stack is the manager
                        )
                    transport = _transport(
                        self.target,
                        env=self._env,
                        cwd=self._cwd,
                        errlog=self._errlog or sys.stderr,
                    )
                    async with client_type(transport, raise_exceptions=True) as client:
                        self._client = client
                        await self._discover()
                        ready.set()
                        await self._stop.wait()
                except Exception as exc:  # noqa: BLE001 — every transport failure lands here
                    self._fail(exc, started=ready.is_set())
        finally:
            self._client = None
            self._errlog = None
            ready.set()

    def _fail(self, exc: Exception, *, started: bool) -> None:
        """Before ready, a failure is `aopen`'s to raise. After, nothing is waiting on
        this task, so it is logged: the server would not close cleanly."""
        stderr = self._server_stderr()
        if started:
            logger.warning(
                "%s: the server did not close cleanly — %s%s",
                self.id,
                _innermost(exc),
                f"\n{stderr}" if stderr else "",
            )
        elif isinstance(exc, ConnectError):
            self._failure = exc
        else:
            message = f"{self.id}: could not start '{self.target}' — {_innermost(exc)}"
            self._failure = ConnectError(f"{message}\n{stderr}" if stderr else message)
            self._failure.__cause__ = exc

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
        # What the server says it is. A model asked the topic of an attached store made
        # no tool call and said it could not tell when nothing in its prompt said.
        self._instructions = str(self._client.instructions or "").strip()[
            :INSTRUCTIONS_MAX
        ]
        info = self._client.server_info
        self._server_title = str(
            (getattr(info, "title", None) or getattr(info, "name", None) or "")
            if info is not None
            else ""
        )
        self._server_name = str(getattr(info, "name", None) or "")

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
            # Declaring the capability is not the same as having any. A server may
            # advertise `resources` and register none, and claiming SEARCH there
            # would put an always-empty heading in every result set.
            if self._resources:
                self._capabilities.extend([Capability.SEARCH, Capability.FETCH])

    async def aclose(self) -> None:
        """Stop the owner task, bounded: a server that will not stop is cancelled.

        Shielded, so a caller cancelled mid-close still takes the subprocess down."""
        owner, self._owner = self._owner, None
        if owner is None:
            return
        assert self._stop is not None
        with anyio.CancelScope(shield=True):
            self._stop.set()
            with anyio.move_on_after(CLOSE_TIMEOUT):
                await asyncio.wait([owner])
            if not owner.done():
                owner.cancel()
                with anyio.move_on_after(CLOSE_TIMEOUT):
                    await asyncio.wait([owner])
        if owner.done():
            self._ended(owner)
            return
        logger.warning(
            "%s: the server did not stop within %ss (asked, then cancelled); "
            "it may still be running",
            self.id,
            2 * CLOSE_TIMEOUT,
        )
        _abandoned.add(owner)
        owner.add_done_callback(self._ended)

    def _ended(self, owner: asyncio.Task[None]) -> None:
        """Log what the owner ended with, if anything `_own` did not catch."""
        _abandoned.discard(owner)
        if owner.cancelled():
            return
        if (exc := owner.exception()) is not None:
            logger.warning(
                "%s: the server did not close cleanly — %s", self.id, _innermost(exc)
            )

    # -- the contract ---------------------------------------------------------

    def describe(self) -> Descriptor:
        return Descriptor(
            id=self.id,
            kind=self.kind,
            title=self._server_title or str(self.target),
            capabilities=list(self._capabilities),
            count=len(self._tools) + len(self._resources) or None,
            unit="items",
            detail={
                # The first paragraph of the server's own instructions: what it holds.
                "description": self._instructions.split("\n\n", 1)[0],
                "server": str(self.target),
                "tools": len(self._tools),
                "resources": len(self._resources),
                "undeclared_effects": sum(
                    1 for t in self._tools if not t.effect_declared
                ),
            },
        )

    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        """Substring match over the resource listing. Filters are refused, not ignored.

        Deliberately shallow: MCP gives a name and a description per resource, not a
        body, so there is nothing deeper to rank on without reading every resource —
        which would be one request each and is what `fetch` is for.
        """
        if given := sorted(
            k for k, v in filters.items() if v not in (None, "", [], {})
        ):
            raise ConnectError(
                f"an MCP resource listing has no fields to filter on "
                f"({', '.join(given)}); ask the agent, which can use the server's tools"
            )
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

    def ref_prefix(self) -> str:
        """The server's own name from its handshake. A server that prints
        `<name>:<key>` gets refs labelled with this source's id in its place."""
        return self._server_name

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
        # snake_case in SDK v2, like `is_error`: the camelCase name read nothing, so
        # every tool reached a model with no parameters at all.
        input_schema=getattr(tool, "input_schema", None) or {},
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
