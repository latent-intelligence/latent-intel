"""Serve one attached source to a subprocess agent, over stdio MCP.

A runtime that owns its own tool loop — `claude-cli` is one — runs in another
process and cannot call our in-process Python. It reaches a source only as an MCP
server. This module is that server, and it works for every connector kind the
client can already read.

**Why it lives here rather than shelling to another package's CLI.** `WikiConnector`
used to return `shutil.which("lw")` as its launch spec, so a store this client reads
perfectly became invisible to the agent whenever a *different* package's console script
was absent from `PATH`. That is a failure the operator did not cause and cannot see: the
config is right, `search` works, and only `ask` is quietly answering without the source.
It fired for real — `uv tool install .` drops a `--with` sibling, and `uv tool install`
links only the *main* package's entry points, so `lw` existed in the tool environment
and still was not on `PATH`.

The rule that replaces it: **a source the client can read, the agent can reach.** The
launcher is `sys.executable -m latent_intel.serve`, which is the interpreter already
running, so it is correct by construction — no second package, no `PATH` lookup, no
version skew, and nothing for a deployment to configure. A path in, tools out.

Tools are not written twice. `Connector.tools()` already declares them with a JSON
schema and `Connector.call()` already dispatches them, because the in-process router
needs both. This module adapts that pair to MCP, so a connector that gains a tool
gains it here with no edit.

Nothing but protocol may reach stdout — that is the transport. Diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path
from typing import Any

from . import env
from .connectors import base as connectors
from .models import Effect, ToolSpec

#: JSON Schema's scalar types, as Python annotations. The server infers a tool's schema
#: from a function signature, so this is how a connector's declared schema is honoured
#: rather than re-derived.
_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

#: What an optional parameter becomes when the schema names no default. Empty rather
#: than `None`: connectors read these with `str(...)` / `int(... or default)`, and a
#: `None` arriving where a string is expected is a bug at the far end of a subprocess
#: boundary.
_EMPTY: dict[type, Any] = {str: "", int: 0, float: 0.0, bool: False, list: [], dict: {}}


def _signature(schema: dict[str, Any]) -> inspect.Signature:
    """A call signature expressing `schema`.

    Required properties become required keyword arguments; the rest take a default, so
    the generated MCP schema marks exactly what the connector marked.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parameters = []
    for name, prop in properties.items():
        annotation = _TYPES.get(str(prop.get("type") or "string"), str)
        if name in required:
            default: Any = inspect.Parameter.empty
        else:
            default = prop.get("default", _EMPTY.get(annotation))
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=default,
            )
        )
    return inspect.Signature(parameters)


def _handler(connector: Any, spec: ToolSpec) -> Any:
    """One MCP tool, delegating to the connector's own dispatcher."""

    async def run(**arguments: Any) -> str:
        return str(await connector.call(spec.name, arguments))

    run.__name__ = spec.name
    run.__doc__ = spec.description or spec.name
    run.__signature__ = _signature(spec.input_schema)  # type: ignore[attr-defined]
    return run


def build_server(connector: Any) -> Any:
    """An MCP server exposing exactly what this connector declares.

    `mcp` is imported here so importing this module stays cheap for callers that only
    want `launch_spec`.
    """
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    descriptor = connector.describe()
    title = descriptor.title or connector.id
    server = MCPServer(
        f"latent-intel:{connector.id}",
        instructions=(
            f"Read access to '{connector.id}' ({descriptor.kind}): {title}, "
            f"{descriptor.count} {descriptor.unit}. Search first to get refs, then "
            f"fetch one in full."
        ),
    )
    for spec in connector.tools():
        # Declared, not implied. A client that gates on effects — ours does — reads this
        # to decide what needs approval, and an absent annotation is correctly read as
        # "assume it writes".
        annotations = ToolAnnotations(
            read_only_hint=not spec.effect.writes,
            open_world_hint=spec.effect is not Effect.NONE,
        )
        server.add_tool(
            _handler(connector, spec),
            name=spec.name,
            description=spec.description,
            annotations=annotations,
        )
    return server


def launch_spec(
    kind: str, target: str, source_id: str, options: dict[str, Any]
) -> dict[str, Any]:
    """The `mcpServers` entry that starts this module for one source.

    `sys.executable` rather than a console script: it is the interpreter already
    running, so the subprocess is the same environment by construction. This is the
    whole reason a source no longer needs a sibling package on `PATH` to be visible.
    """
    args = ["-m", "latent_intel.serve", "--kind", kind, "--target", target]
    args += ["--id", source_id]
    for key, value in options.items():
        args += ["--option", f"{key}={value}"]
    # The *path*, never the values. This block is handed to `claude` as a command-line
    # argument, so a credential placed here would show up in `ps`. It is also why MCP's
    # env whitelist — HOME, LOGNAME, PATH, SHELL, TERM, USER, and nothing else — cannot
    # silently strip the credentials a served source needs: the child re-reads the file.
    for path in env.files():
        args += ["--env-file", str(path)]
    return {"command": sys.executable, "args": args}


def _options(pairs: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--option expects key=value, got {pair!r}")
        parsed[key.strip()] = value
    return parsed


async def _run(kind: str, target: str, source_id: str, options: dict[str, Any]) -> None:
    connector = connectors.build(kind, source_id, target, **options)
    await connectors.open_connector(connector)
    if not isinstance(connector, connectors.ToolProvider):
        raise SystemExit(f"{kind} sources offer no tools, so there is nothing to serve")
    await build_server(connector).run_stdio_async()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="latent-intel serve",
        description="Serve one source to a subprocess agent over stdio MCP.",
    )
    parser.add_argument(
        "--kind", required=True, help="connector kind, e.g. wiki, files"
    )
    parser.add_argument("--target", required=True, help="path or URI of the source")
    parser.add_argument("--id", default="", help="source id; defaults to the kind")
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="connector option, repeatable (e.g. --option pattern='**/*.md')",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        metavar="PATH",
        help="a .env to apply before connecting, repeatable, nearest first",
    )
    parsed = parser.parse_args(argv)

    # The parent passed paths rather than values. Apply them here, still never
    # overriding a variable this process was actually given.
    if parsed.env_file:
        env.apply([Path(p) for p in parsed.env_file])

    import anyio

    try:
        anyio.run(
            _run,
            parsed.kind,
            parsed.target,
            parsed.id or parsed.kind,
            _options(parsed.option),
        )
    except KeyboardInterrupt:  # pragma: no cover - a signal, not a path
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
