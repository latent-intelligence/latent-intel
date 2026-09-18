"""Plumbing both terminal frontends share. Only frontends touch stdout.

The one thing worth reading is `stream`: where the two output modes diverge, and the
only place they may. `--json` prints raw envelope-stamped events; the default renders
them. Both consume the same iterator, so a scripted pipeline and a person at a terminal
are looking at the same thing — and `--json` doubles as the serialization proof.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from .. import config as config_module
from .. import env as env_module
from .. import events as ev
from .. import settings as settings_module
from ..commands import Command
from ..models import Descriptor, SourceRequest
from ..session import Session
from ..ui import brand as brand_module
from ..ui import render as render_module
from ..ui import theme as theme_module
from ..ui.theme import THEME

console = Console(theme=THEME)
err_console = Console(theme=THEME, stderr=True)


async def open_session() -> tuple[Session, list[str]]:
    """A session with every configured source attached.

    A CLI is a new process each time, so "attached" lives in the config file. A source
    that fails is reported and skipped rather than fatal — one unreachable Drive path
    should not stop a search over the other four.
    """
    session = Session()
    requests = [
        SourceRequest(
            spec=spec.spec,
            # A registry id carries its own kind; a literal path does not.
            kind=None if spec.by_name else spec.kind,
            source_id=spec.id,
            remote=spec.remote,
            options=spec.options,
        )
        for spec in settings_module.load().sources
    ]
    return session, await session.connect_many(requests)


async def stream(
    session: Session,
    command: Command,
    *,
    as_json: bool = False,
    record_to: Path | None = None,
) -> bool:
    """Run one command, rendering or dumping as it goes. True when nothing failed.

    `record_to` tees rather than forks: the run still renders to the terminal, and one
    JSON line — the same line `--json` prints — is appended per event. Appended and
    flushed as it happens, so an interrupted run replays up to the interruption, which
    is the common case rather than the exotic one.

    `record_to`, not `record`: `record` in this module writes a source to the config,
    and one name for two unrelated things is how a later reader loses an afternoon.
    """
    ok = True
    recording = record_to.open("a", encoding="utf-8") if record_to else None
    try:
        async for event in session.run(command):
            if isinstance(event, ev.AgentFailed):
                ok = False
            if as_json:
                print(ev.dump_event(event))
            else:
                render_module.render(console, event)
            if recording is not None:
                recording.write(ev.dump_event(event) + "\n")
                recording.flush()
    finally:
        if recording is not None:
            recording.close()
    return ok


def print_hosts() -> None:
    """Every endpoint each runtime can reach, and what each one needs.

    Here rather than in either frontend because both ask it — `intel hosts` and
    `/hosts` — and a table phrased two ways is two tables to keep in step. Synchronous
    and sessionless: it reads declarations and the environment, and attaching sources
    to answer a question about endpoints would make diagnosis slower than the thing
    being diagnosed.

    **`!` is reserved for what is actually in your way.** Every unconfigured endpoint
    carried one before, so a machine that had finished setting up one host still showed
    six warnings and a reader had no way to tell which mattered. An endpoint nobody
    chose is not a fault, and now reads `·` — the mark `doctor` already uses for a
    connector that is not installed.

    **Only the runtime that answers is marked configured.** A runtime resolves a host
    whether or not it is the one in use, so marking each runtime's resolved host said
    "configured" about endpoints the reader had never chosen — beside the one they
    actually had. The heading carries the marker now, and the host marker is `in use`,
    which is true only under the heading that says so.
    """
    active = settings_module.load().runtime

    for kind in sorted(Session.runtime_status()):
        report = Session.host_report(kind)
        chosen = kind == active
        heading = f"[accent.strong]{escape(kind)}[/]"
        if chosen:
            heading += " [dim]← configured[/]"
        if not report.hosts:
            # `claude-cli` has no table. Saying so beats omitting the runtime, which
            # reads as "not installed" next to two that are listed.
            console.print(f"{heading} [dim]— no hosts[/]\n")
            continue
        console.print(heading)
        # The runtime's own reason belongs here only when it is not one of the rows
        # below: a missing SDK or an unset model stops every host at once, while a
        # configured host's missing variables are already printed against that host.
        # Derived rather than flagged, so the two can never both claim the same line.
        in_force = report.hosts.get(report.configured or "")
        if report.reason and (in_force is None or in_force.reason is None):
            console.print(f"  [warn]![/] [dim]{escape(report.reason)}[/]")
        width = max(len(name) for name in report.hosts)
        for name, status in report.hosts.items():
            in_use = chosen and name == report.configured
            if status.reason is None:
                mark = "[ok]✓[/]"
            elif in_use:
                mark = "[warn]![/]"
            else:
                mark = "[dim]·[/]"
            marker = "[dim]← in use[/]  " if in_use else ""
            # What a ✓ stands on, for the same reason a failure names variables: a row
            # answered through a fallback is being read from a name it never asked for.
            detail = (
                f"[dim]{escape(', '.join(status.variables))}[/]"
                if status.reason is None
                else f"[dim]{escape(status.reason)}[/]"
            )
            pad = " " * (width - len(name))
            row = f"  {mark} [source]{escape(name)}[/]{pad}  {marker}{detail}"
            console.print(row.rstrip())
        console.print()

    console.print(
        "[dim]Names of variables, never values — safe to paste. Set them in a `.env` "
        "beside the project file, or export them.[/]"
    )


def report(problems: list[str]) -> None:
    """Attach failures go to stderr, so `--json` on stdout stays machine-readable."""
    for problem in problems:
        err_console.print(f"[warn]![/] {escape(problem)}")


@asynccontextmanager
async def session_scope() -> AsyncIterator[tuple[Session, list[str]]]:
    """A session that always closes, however the body ends.

    Not a nicety. An MCP source is a subprocess, and `aclose` is what terminates it —
    so a command that raised on its way out left a server running. That happened for
    real: `intel stores | head -3` closed the pipe, `BrokenPipeError` skipped the
    cleanup at the end of the function, and the orphaned server inherited the
    pipeline's stdout, so the shell never saw EOF and hung.
    """
    session, problems = await open_session()
    try:
        yield session, problems
    finally:
        await session.aclose()


def load_env() -> list[Path]:
    """Apply the active deployment's `.env`, then the machine-wide one.

    Deliberately *after* the project is resolved and *before* anything reads a
    credential: the project's directory is where its `.env` lives. The consequence,
    stated so nobody debugs it twice: a `.env` cannot choose the active project — that
    is `intel project use`, which persists. It can set anything read later, so settings
    are invalidated afterwards.
    """
    resolved = settings_module.load()
    directory = resolved.project.directory if resolved.project else None
    loaded = env_module.load(directory, config_module.home())
    if loaded:
        settings_module.invalidate()
    return loaded


def active_brand() -> brand_module.Brand:
    """The brand for the active project, or Latent's."""
    resolved = settings_module.load()
    if resolved.project is None:
        return brand_module.DEFAULT
    return brand_module.from_project(
        resolved.project.branding, resolved.project.directory
    )


def use_brand(brand: brand_module.Brand, overrides: dict[str, str]) -> list[str]:
    """Retheme both consoles for this process. Returns unrecognised token names.

    `push_theme` rather than rebuilding the Console: the two consoles are module-level
    singletons created at import, and every renderer already holds a reference to them.
    """
    merged = {**brand.theme, **overrides}
    theme, unknown = theme_module.build_theme(merged)
    if merged:
        console.push_theme(theme)
        err_console.push_theme(theme)
    return unknown


def record(
    spec: str,
    descriptor: Descriptor,
    *,
    remote: bool,
    by_name: bool,
    options: dict[str, Any] | None = None,
) -> Path:
    """Write an attached source to the config, as it was named.

    `by_name` is what keeps this portable: a registry id is stored as `from:` and
    re-resolved wherever the config is read, while a literal path is stored verbatim.
    Neither stores what the path resolved to — that is machine-specific and, for a Drive
    mount, personally identifying.

    It writes the **user's overlay for the active project**, never the project file.
    That file belongs to whoever owns the deployment, and a tool that edited it would
    make a checked-out project repo dirty on the first `connect`.

    `options` are written with the source, so a source that needed them to connect
    reconnects without them being retyped — an MCP server attached with `env` and `cwd`
    was silently recorded without either, and failed to start on the next command.
    """
    cfg = config_module.load()
    cfg.add(
        config_module.SourceSpec(
            id=descriptor.id,
            kind=descriptor.kind,
            from_registry=spec if by_name else None,
            target=None if by_name else spec,
            remote=remote,
            options=dict(options or {}),
        ),
        settings_module.load().project_name,
    )
    return config_module.save(cfg)
