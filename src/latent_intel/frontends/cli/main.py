"""`intel` — the scriptable half.

Bare `intel` opens the interactive shell; everything else is an ordinary subcommand that
runs and exits. Both consume the same event stream from the same `Session`, so the two
never disagree about what a search returned.

`--json` on any streaming command emits the raw events, envelope and all. That is the
scripting path, and it is also how a web client will eventually be fed.
"""

from __future__ import annotations

import anyio
import typer
from rich.markup import escape

from ... import __version__
from ... import config as config_module
from ... import settings as settings_module
from ...commands import Ask, Connect, Fetch, Find
from ...models import SessionError
from .. import _shared
from .._shared import (
    console,
    err_console,
    record,
    report,
    session_scope,
    stream,
)

app = typer.Typer(
    name="intel",
    help=__doc__,
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=False,
)


from . import project as project_cli  # noqa: E402

app.add_typer(project_cli.app)


@app.callback()
def root(ctx: typer.Context) -> None:
    """Open the interactive shell when called with no subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    from ..shell.repl import run as run_shell

    anyio.run(run_shell)


# -- sources ----------------------------------------------------------------


@app.command()
def stores() -> None:
    """What is attached, and what else the registry knows about."""
    anyio.run(_stores)


async def _stores() -> None:
    async with session_scope() as (session, problems):
        # Before the listing, not after: a source that failed to attach matters more
        # than the ones that worked, and `intel stores | head` hides whatever is last.
        report(problems)
        attached = {d.id for d in session.sources()}

        if attached:
            console.print("[accent.strong]attached[/]")
            for d in session.sources():
                size = f"{d.count} {d.unit}" if d.count is not None else ""
                caps = " ".join(str(c) for c in d.capabilities)
                fresh = f"  [dim]built {d.freshness}[/]" if d.freshness else ""
                console.print(
                    f"  [source]{d.id:<18}[/] [dim]{d.kind:<8}[/] "
                    f"[text]{size:<14}[/] [dim]{caps}[/]{fresh}"
                )
        else:
            console.print("[dim]nothing attached[/]")

        known = [d for d in session.available() if d.id not in attached]
        if known:
            console.print("\n[accent.strong]available[/] [dim](intel connect <id>)[/]")
            # Clipped to what is left on the line, so a long domain wraps into a
            # second ragged row instead of being cut mid-word by the terminal.
            room = max(console.size.width - 32, 20)
            for d in known:
                mark = (
                    "" if d.detail.get("connectable") else "  [dim]— not connectable[/]"
                )
                domain = " ".join(str(d.detail.get("domain") or "").split())
                if len(domain) > room:
                    domain = domain[: room - 1] + "…"
                console.print(
                    f"  [source]{d.id:<18}[/] [dim]{d.kind:<8} {domain}[/]{mark}"
                )


@app.command()
def connect(
    spec: str = typer.Argument(..., help="A registered store id, or a path/URI."),
    kind: str = typer.Option(
        None, "--kind", help="wiki | files | mcp | vector. Required for a raw path."
    ),
    name: str = typer.Option(None, "--as", help="Attach it under a different id."),
    remote: bool = typer.Option(
        False, "--remote", help="Use the registry's mirror rather than the local copy."
    ),
) -> None:
    """Attach a source, and remember it for later commands."""
    anyio.run(lambda: _connect(spec, kind, name, remote))


async def _connect(
    spec: str, kind: str | None, name: str | None, remote: bool = False
) -> None:
    async with session_scope() as (session, _):
        try:
            descriptor = await session.connect(
                spec, kind=kind, source_id=name, remote=remote
            )
        except SessionError as exc:
            err_console.print(f"[fail]✗[/] {escape(str(exc))}")
            raise typer.Exit(1) from exc

        path = record(spec, descriptor, remote=remote, by_name=kind is None)

        size = f"{descriptor.count} {descriptor.unit}" if descriptor.count else ""
        caps = " ".join(str(c) for c in descriptor.capabilities)
        console.print(
            f"[ok]✓[/] [dim]{descriptor.kind:<6}[/] [source]{descriptor.id}[/]  "
            f"[text]{size}[/]  [dim]{caps}[/]"
        )
        console.print(f"[dim]recorded in {escape(str(path))}[/]")


@app.command()
def disconnect(source_id: str = typer.Argument(..., help="The id to detach.")) -> None:
    """Detach a source and forget it."""
    cfg = config_module.load()
    if not cfg.remove(source_id):
        err_console.print(f"[fail]✗[/] '{escape(source_id)}' is not attached")
        raise typer.Exit(1)
    config_module.save(cfg)
    console.print(f"[dim]detached[/] [source]{escape(source_id)}[/]")


# -- reading ----------------------------------------------------------------


@app.command()
def search(
    query: str = typer.Argument(..., help="What to look for."),
    source: str = typer.Option(None, "--source", help="Search one source only."),
    limit: int = typer.Option(10, "--limit", help="Hits per source."),
    kind: str = typer.Option(
        None, "--kind", help="Filter by type, where a source supports it."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw events."),
) -> None:
    """Search every attached source. Results group by source and are never merged."""
    filters = {"type": kind} if kind else {}
    command = Find(query=query, source=source, limit=limit, filters=filters)
    anyio.run(lambda: _stream(command, as_json))


@app.command()
def get(
    ref: str = typer.Argument(
        ..., help="source:key — or a bare key for the first source."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw events."),
) -> None:
    """Fetch one document in full."""
    anyio.run(lambda: _stream(Fetch(ref=ref), as_json))


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="A question for the agent."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw events."),
) -> None:
    """Ask the agent. Reports clearly that no runtime is configured yet."""
    anyio.run(lambda: _stream(Ask(prompt=prompt), as_json))


async def _stream(command: Connect | Find | Fetch | Ask, as_json: bool) -> None:
    async with session_scope() as (session, problems):
        report(problems)
        ok = await stream(session, command, as_json=as_json)
    if not ok:
        raise typer.Exit(1)


@app.command()
def run(
    name: str = typer.Argument(None, help="A project command. Omit to list them."),
    argument: list[str] = typer.Argument(None, help="Passed as {{argument}}."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw events."),
) -> None:
    """Run one of the active project's commands.

    Always available and unambiguous, so a project command works even when its name
    collides with something built in.
    """
    from ... import procedures as procedures_module

    resolved = settings_module.load()
    catalogue, problems = procedures_module.discover(
        resolved.project.commands_dir if resolved.project else None
    )
    report(problems)

    if not name:
        if not catalogue:
            console.print("[dim]this project defines no commands[/]")
            return
        for command_name, procedure in sorted(catalogue.items()):
            console.print(
                f"  [tool]{command_name:<14}[/] [dim]{escape(procedure.description)}[/]"
            )
        return

    chosen = catalogue.get(name)
    if chosen is None:
        known = ", ".join(sorted(catalogue)) or "none"
        err_console.print(
            f"[fail]✗[/] no command '{escape(name)}' [dim](defined: {escape(known)})[/]"
        )
        raise typer.Exit(1)

    try:
        commands = procedures_module.compile(
            chosen,
            " ".join(argument or []),
            project=resolved.project_name or "",
            among=catalogue,
        )
    except procedures_module.ProcedureError as exc:
        err_console.print(f"[fail]✗[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc

    for command in commands:
        anyio.run(lambda c=command: _stream(c, as_json))  # type: ignore[misc]


# -- diagnosis --------------------------------------------------------------


@app.command()
def doctor() -> None:
    """What is installed, what is reachable, and what is not declared."""
    anyio.run(_doctor)


async def _doctor() -> None:
    from ...session import Session

    console.print(f"[accent.strong]latent-intel[/] [dim]{__version__}[/]\n")

    kinds = Session.connector_kinds()
    console.print("[accent.strong]connectors[/] [dim](entry points)[/]")
    for name in kinds:
        console.print(f"  [ok]✓[/] [source]{name}[/]")
    for name in ("files", "mcp", "wiki", "vector"):
        if name not in kinds:
            console.print(f"  [dim]·[/] [dim]{name} — not installed[/]")

    console.print("\n[accent.strong]runtimes[/]")
    runtimes = Session.runtime_kinds()
    configured = config_module.load().runtime
    for name, usable in sorted(runtimes.items()):
        mark = "[ok]✓[/]" if usable else "[warn]![/]"
        note = "" if usable else " [dim]— installed, but cannot run here[/]"
        chosen = " [dim]← configured[/]" if name == configured else ""
        console.print(f"  {mark} [source]{name}[/]{note}{chosen}")
    for name in ("claude-cli", "api", "openrouter"):
        if name not in runtimes:
            console.print(f"  [dim]·[/] [dim]{name} — declared, not implemented[/]")
    if configured and (model := config_module.load().model_for(configured)):
        console.print(f"  [dim]model:[/] [source]{model}[/]")

    async with session_scope() as (session, problems):
        console.print("\n[accent.strong]attached[/]")
        # An agent that owns its own tool loop reaches a source only as an MCP server.
        # `files` and `vector` are in-process and have none, so say which sources the
        # agent can actually see rather than implying it sees everything attached.
        servable = set(session.mcp_servers())
        for d in session.sources():
            reach = "" if d.id in servable else " [dim](not visible to the agent)[/]"
            console.print(f"  [ok]✓[/] [source]{d.id}[/] [dim]{d.kind}[/]{reach}")
        if not session.sources():
            console.print("  [dim]nothing attached[/]")
        report(problems)

        undeclared = [t for t in session.tools() if not t.effect_declared]
        if undeclared:
            console.print("\n[warn]tools with no declared effect[/]")
            console.print(
                "  [dim]treated as external_write until they say otherwise[/]"
            )
            for tool in undeclared:
                console.print(f"  [warn]![/] [tool]{tool.qualified}[/]")

    console.print(f"\n[dim]config  {escape(str(config_module.config_path()))}[/]")
    console.print(f"[dim]data    {escape(str(config_module.data_home()))}[/]")


@app.command()
def version() -> None:
    """Print the version and the event schema it speaks."""
    from ... import events as ev

    console.print(f"latent-intel {__version__} (event schema v{ev.SCHEMA_VERSION})")


if __name__ == "__main__":  # pragma: no cover
    app()


def main() -> None:
    """The console-script entry.

    Anything that must happen between import and dispatch happens here, because there is
    nowhere else it can: `pyproject` used to name the Typer `app` object directly, so no
    code of ours ran before Click took over.

    A broken project must not brick the tool — `settings.load()` degrades to engine
    defaults and carries the error, so `intel doctor` still works on the machine whose
    project file is malformed. That is the machine where you need it.
    """
    # Before `settings.load()`: a `.env` may name the active project, the registry, or
    # the credentials a source needs, and settings caches what it reads.
    _shared.load_env()

    brand = _shared.active_brand()
    unknown = _shared.use_brand(brand, settings_module.load().theme)
    for token in unknown:
        err_console.print(f"[warn]![/] [dim]unknown theme token: {escape(token)}[/]")
    app()
