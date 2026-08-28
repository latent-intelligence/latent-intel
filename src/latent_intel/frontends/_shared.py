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


async def stream(session: Session, command: Command, *, as_json: bool = False) -> bool:
    """Run one command, rendering or dumping as it goes. True when nothing failed."""
    ok = True
    async for event in session.run(command):
        if isinstance(event, ev.AgentFailed):
            ok = False
        if as_json:
            print(ev.dump_event(event))
        else:
            render_module.render(console, event)
    return ok


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


def record(spec: str, descriptor: Descriptor, *, remote: bool, by_name: bool) -> Path:
    """Write an attached source to the config, as it was named.

    `by_name` is what keeps this portable: a registry id is stored as `from:` and
    re-resolved wherever the config is read, while a literal path is stored verbatim.
    Neither stores what the path resolved to — that is machine-specific and, for a Drive
    mount, personally identifying.

    It writes the **user's overlay for the active project**, never the project file.
    That file belongs to whoever owns the deployment, and a tool that edited it would
    make a checked-out project repo dirty on the first `connect`.
    """
    cfg = config_module.load()
    cfg.add(
        config_module.SourceSpec(
            id=descriptor.id,
            kind=descriptor.kind,
            from_registry=spec if by_name else None,
            target=None if by_name else spec,
            remote=remote,
        ),
        settings_module.load().project_name,
    )
    return config_module.save(cfg)
