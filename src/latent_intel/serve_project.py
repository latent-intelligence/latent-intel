"""`intel serve --deployment X` — one project, served over MCP, stdio or HTTP.

`serve.py` serves one source to a subprocess agent. This serves a whole deployment to
any MCP client — Claude Code, Claude Desktop, a Copilot-class workbench — from the
project file that already drives the CLI: sources, persona, skills, commands, views.
Nothing is configured twice. The client's `.mcp.json` only says how to launch this
(stdio) or where to reach it (HTTP); `client_config` writes that block.

**Only sources we own are served.** A source that resolves to an MCP server — declared
`kind: mcp`, or a registry name whose entry is one — is someone else's server; a client
should attach it directly, so it is skipped and named rather than proxied. A proxy
would blur whose read/write annotations apply.

**The tools are the connectors' own.** Each tool is the same `tools()`/`call()` pair
the in-process router uses, so the server and `intel ask` cannot drift. With one
tool-providing source the names stay as declared (`wiki_search`); with several they
are prefixed `<source>__`, and the instructions name them the way they are registered.

**HTTP requires a token,** read from the environment variable the project names in
`serve.auth.token_env` — never from the project file — checked before any source is
opened and compared in constant time on every request. HTTP without one is refused
unless the operator passes `--insecure-local` and the host is loopback.

This module reports through a `notify` callback and raises `ServeError`; only the CLI
prints.
"""

from __future__ import annotations

import hmac
import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import procedures, registry, views
from .agent import tools as engine_tools
from .connectors import base as connectors
from .project import Project, View
from .serve import add_tools

#: Hosts that only this machine can reach — the one place HTTP may run without a token.
LOOPBACK = ("127.0.0.1", "localhost", "::1")

#: Bind addresses that mean "every interface" — not an address a client can dial.
WILDCARD = ("0.0.0.0", "::", "")

Notify = Callable[[str], None]


class ServeError(Exception):
    """A deployment that cannot be served as asked. The message says why."""


@dataclass
class Served:
    """What `intel serve` attached, and what it left out and why."""

    connectors: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    #: The registered name of each tool, by source id and the tool's own name.
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    trees: dict[str, views.Tree] = field(default_factory=dict)


def _resolved(project: Project, spec: Any) -> tuple[str, str]:
    if spec.target:
        return spec.kind, spec.target
    return registry.resolve(
        str(spec.from_registry), project.registry, remote=spec.remote
    )


async def open_sources(project: Project) -> Served:
    """Attach the project's own sources. One that fails is reported, not fatal."""
    served = Served()
    for spec in project.sources:
        try:
            kind, target = _resolved(project, spec)
        except Exception as exc:  # noqa: BLE001 — one bad source must not stop the rest
            served.skipped.append(f"{spec.id}: {exc}")
            continue
        if kind == "mcp":
            served.skipped.append(
                f"{spec.id}: a third-party MCP server — attach it to the client "
                f"directly (`--print-config` lists it)"
            )
            continue
        try:
            connector = connectors.build(kind, spec.id, target, **spec.options)
            served.connectors[spec.id] = await connectors.open_connector(connector)
        except Exception as exc:  # noqa: BLE001
            served.skipped.append(f"{spec.id}: {exc}")
    return served


def tool_name(source_id: str, name: str, several: bool) -> str:
    return f"{source_id}__{name}" if several else name


def instructions(project: Project, served: Served) -> str:
    """What a client reads first: the deployment's voice, then how to climb."""
    title = project.title + (f" — {project.description}" if project.description else "")
    lines = [title]
    if project.persona:
        lines += ["", project.persona]
    lines += ["", "Sources:"]
    for source_id, connector in served.connectors.items():
        d = connector.describe()
        about = d.detail.get("description") or d.title
        lines.append(f"- {source_id} ({d.kind}, {d.count} {d.unit}): {about}")
    for source_id, connector in served.connectors.items():
        if connector.kind != "wiki":
            continue
        named = served.names.get(source_id, {})

        def n(tool: str, names: dict[str, str] = named) -> str:
            return names.get(tool, tool)

        lines += [
            "",
            f"How to read {source_id}: start with {n('wiki_overview')}; find with "
            f"{n('wiki_search')} (filters: type, tag, part_of, where); read with "
            f"{n('wiki_get')} (section= for one part); see what a page rests on with "
            f"{n('wiki_evidence')}; read source text with {n('wiki_raw')} only when "
            "that does not answer. Cite page keys and evidence entries. Pages marked "
            "draft rest on one source; pages marked inferred are the author's pattern, "
            "not a source's statement.",
        ]
    if project.views:
        lines.append(
            "Structured views: "
            + ", ".join(v.name for v in project.views)
            + " — wiki_view name=<view> [root=<key>] [depth=<n>]."
        )
    skills = engine_tools.loadable(project.skills)
    if skills:
        lines += ["", "Skills (load with load_skill, or read skill://<name>/SKILL.md):"]
        lines += [f"- {s.name}: {s.description}" for s in skills]
    return "\n".join(lines)


def build(project: Project, served: Served) -> Any:
    """The MCP server for a project whose sources are already open."""
    from mcp.server import MCPServer
    from mcp.server.mcpserver.prompts.base import Prompt
    from mcp.server.mcpserver.resources import FunctionResource
    from mcp.types import ToolAnnotations

    from .serve_skills import SkillsExtension

    providers = {
        source_id: c
        for source_id, c in served.connectors.items()
        if isinstance(c, connectors.ToolProvider)
    }
    several = len(providers) > 1
    for source_id, connector in providers.items():
        served.names[source_id] = {
            spec.name: tool_name(source_id, spec.name, several)
            for spec in connector.tools()
        }

    server = MCPServer(
        f"latent-intel:{project.name}",
        title=project.title,
        instructions=instructions(project, served),
        extensions=[SkillsExtension(project.skills)] if project.skills else [],
    )
    for source_id, connector in providers.items():
        add_tools(server, connector, prefix=f"{source_id}__" if several else "")

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    loader = engine_tools.specs(project.skills)
    if loader:
        server.add_tool(
            _load_skill(project.skills),
            name=engine_tools.LOAD_SKILL,
            description=loader[0].description,
            annotations=read_only,
        )

    for view in project.views:
        connector = _source_for(view, served)
        if connector is not None:
            served.trees[view.name] = views.build(view, _pages(connector))
            server.add_resource(
                FunctionResource.from_function(
                    _view_resource(served.trees[view.name]),
                    uri=f"view://{view.name}",
                    name=view.name,
                    description=f"The {view.name} view as data",
                    mime_type="application/json",
                )
            )
    if project.views:
        server.add_tool(
            _view_tool(project.views, served),
            name="wiki_view",
            description=(
                "A declared structure over the wiki, as an indented tree: the roots, "
                "their children in order, and the fields the view carries. Pass root= "
                "for one subtree and depth= to stop early. Views: "
                + ", ".join(v.name for v in project.views)
            ),
            annotations=read_only,
        )

    for skill in project.skills:
        if skill.user_invocable:
            server.add_prompt(
                Prompt.from_function(
                    _skill_prompt(skill.body),
                    name=skill.name,
                    description=skill.description,
                )
            )
    commands, _ = procedures.discover(project.commands_dir)
    taken = {s.name for s in project.skills if s.user_invocable}
    for procedure in commands.values():
        if procedure.name in taken:
            continue
        server.add_prompt(
            Prompt.from_function(
                _procedure_prompt(procedure, project.name, commands),
                name=procedure.name,
                description=procedure.description or procedure.usage,
            )
        )
    return server


def _load_skill(skills: list[Any]) -> Any:
    async def load_skill(name: str) -> str:
        return await engine_tools.call(skills, engine_tools.LOAD_SKILL, {"name": name})

    return load_skill


def _source_for(view: View, served: Served) -> Any:
    if view.source:
        return served.connectors.get(view.source)
    wikis = [c for c in served.connectors.values() if c.kind == "wiki"]
    return wikis[0] if len(wikis) == 1 else None


def _pages(connector: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    return {key: (page.title, page.raw) for key, page in connector.pages.items()}


def _view_resource(tree: views.Tree) -> Callable[[], str]:
    def read() -> str:
        # Frontmatter dates arrive as `date` objects from a library-loaded store.
        return json.dumps(views.as_dict(tree), indent=1, default=str)

    return read


def _view_tool(declared: list[View], served: Served) -> Any:
    async def wiki_view(name: str, root: str = "", depth: int = 0) -> str:
        if name not in {v.name for v in declared}:
            return f"no view '{name}'; views: {', '.join(v.name for v in declared)}"
        tree = served.trees.get(name)
        if tree is None:
            return f"view '{name}': its source is not attached"
        return views.render(tree, root, max(0, depth))

    return wiki_view


def _skill_prompt(body: str) -> Callable[..., str]:
    def prompt(arguments: str = "") -> str:
        return body.replace("$ARGUMENTS", arguments)

    return prompt


def _procedure_prompt(
    procedure: procedures.Procedure, project_name: str, catalogue: dict[str, Any]
) -> Callable[..., str]:
    """A saved command as a prompt. A required argument is required in its schema."""

    def render(argument: str) -> str:
        try:
            compiled = procedures.compile(
                procedure, argument, project=project_name, among=catalogue
            )
        except procedures.ProcedureError as exc:
            return str(exc)
        lines = []
        for command in compiled:
            if command.type == "ask":
                lines.append(command.prompt)
            elif command.type == "find":
                where = f" in {command.source}" if command.source else ""
                lines.append(
                    f"Search{where} for {command.query!r} (limit {command.limit}) "
                    f"and summarise what the results establish."
                )
        return "\n\n".join(lines)

    if procedure.argument_required:

        def required(argument: str) -> str:
            return render(argument)

        return required

    def optional(argument: str = "") -> str:
        return render(argument)

    return optional


# -- running --------------------------------------------------------------------


class BearerCheck:
    """ASGI middleware: refuse any request whose bearer token is not the one set.

    Everything but the lifespan protocol is checked — a websocket route mounted later
    must not be the way around the token. The scheme name is case-insensitive
    (RFC 7235); the token is compared in constant time.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        scheme, _, given = headers.get(b"authorization", b"").partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(given, self._token):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": b"unauthorized"})


def http_app(server: Any, token: str | None, host: str) -> Any:
    app = server.streamable_http_app(stateless_http=True, host=host)
    return BearerCheck(app, token) if token else app


def token_for(project: Project) -> tuple[str | None, str]:
    """The token, and the variable it came from. The value never leaves the env."""
    name = str((project.serve.get("auth") or {}).get("token_env") or "")
    return (os.environ.get(name) or None) if name else None, name


async def run(
    project: Project,
    *,
    transport: str,
    host: str,
    port: int,
    insecure_local: bool = False,
    notify: Notify = lambda line: None,
) -> None:
    """Serve until the client goes away (stdio) or the process is stopped (HTTP)."""
    token: str | None = None
    if transport == "http":
        token, variable = token_for(project)
        if token is None and not (insecure_local and host in LOOPBACK):
            what = f"${variable} is not set" if variable else "no serve.auth.token_env"
            raise ServeError(
                f"refusing HTTP without a token ({what}). Set one, or pass "
                f"--insecure-local with a loopback host for a local test."
            )
    served = await open_sources(project)
    try:
        for line in served.skipped:
            notify(f"skipped {line}")
        if not served.connectors:
            raise ServeError(f"{project.name}: no source could be served")
        server = build(project, served)
        notify(
            f"serving {project.name} over {transport}: {', '.join(served.connectors)}"
        )
        if transport == "stdio":
            await server.run_stdio_async()
            return
        import uvicorn

        config = uvicorn.Config(
            http_app(server, token, host), host=host, port=port, log_level="warning"
        )
        await uvicorn.Server(config).serve()
    finally:
        await _close(served, notify)


async def _close(served: Served, notify: Notify) -> None:
    """Close every connector, even if one fails or the task is being cancelled."""
    import anyio

    with anyio.CancelScope(shield=True):
        for source_id, connector in served.connectors.items():
            try:
                await connector.aclose()
            except Exception as exc:  # noqa: BLE001 — close the rest regardless
                notify(f"closing {source_id}: {exc}")


def client_config(
    project: Project,
    *,
    transport: str,
    host: str,
    port: int,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """The `mcpServers` block a client needs. A token is a variable, never a value.

    Third-party MCP sources in the project — declared `kind: mcp` or resolved to one
    through the registry — are listed beside ours with their own launch line or URL, so
    one paste sets a client up with everything the deployment uses; they stay separate
    servers.
    """
    name = f"intel-{project.name}"
    entry: dict[str, Any]
    if transport == "http":
        _, variable = token_for(project)
        dial = "<host>" if host in WILDCARD else host
        dial = f"[{dial}]" if ":" in dial else dial
        entry = {"type": "http", "url": f"http://{dial}:{port}/mcp"}
        if variable:
            entry["headers"] = {"Authorization": f"Bearer ${{{variable}}}"}
    else:
        argv = command or ["intel"]
        entry = {
            "command": argv[0],
            "args": [*argv[1:], "serve", "--deployment", str(project.path)],
        }
    servers: dict[str, Any] = {name: entry}
    for spec in project.sources:
        try:
            kind, target = _resolved(project, spec)
        except Exception:  # noqa: BLE001 — an unresolvable source has no launch line
            continue
        if kind != "mcp" or not target:
            continue
        if "://" in target:
            servers[spec.id] = {"type": "http", "url": target}
        else:
            parts = shlex.split(target)
            servers[spec.id] = {"command": parts[0], "args": parts[1:]}
    return {"mcpServers": servers}
