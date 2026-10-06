"""Plumbing both terminal frontends share. Only frontends touch stdout.

The one thing worth reading is `stream`: where the two output modes diverge, and the
only place they may. `--json` prints raw envelope-stamped events; the default renders
them. Both consume the same iterator, so a scripted pipeline and a person at a terminal
are looking at the same thing — and `--json` doubles as the serialization proof.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
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


@contextmanager
def recording(path: Path | None) -> Iterator[Callable[[ev.BaseEvent], None]]:
    """A tee: yields a callable that appends one JSON line per event to `path`.

    Tees rather than forks — the caller still renders — and appends and flushes as it
    happens, so an interrupted run replays up to the interruption, which is the common
    case rather than the exotic one. `None` yields a no-op, so a caller never branches
    on whether a recording is in force.

    Entered by the caller rather than by `stream`, because a path that cannot be opened
    has to be a clean error *before* a single source is attached.

    `recording`, not `record`: `record` in this module writes a source to the config,
    and one name for two unrelated things is how a later reader loses an afternoon.
    """
    if path is None:
        yield lambda event: None
        return
    if path.parent == recordings_home():
        # Ours to create, unlike a directory someone typed: a typo there stays an error.
        path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    try:

        def write(event: ev.BaseEvent) -> None:
            handle.write(ev.dump_event(event) + "\n")
            handle.flush()

        yield write
    finally:
        handle.close()


def recordings_home() -> Path:
    """Where a recording named without a suffix or a directory lives."""
    return config_module.data_home() / "recordings"


def recording_path(name: str | Path) -> Path:
    """Where a recording called `name` lives. Resolves; creates nothing.

    A plain name — no suffix, no directory, no `~` — is a recording in the data
    directory, so `/record demo` and `/replay demo` meet wherever each was typed.
    Anything else is a file and is used as typed: `run.jsonl` is in the working
    directory, as it always was, and so is a `--json` capture someone wants replayed.
    """
    text = str(name)
    path = Path(text).expanduser()
    if path.suffix or path.is_absolute() or any(c in text for c in "/\\~"):
        return path
    return recordings_home() / path.with_suffix(".jsonl")


def same_context(a: ev.RunContext | None, b: ev.RunContext) -> bool:
    """Whether two headers describe the same setup, envelopes aside."""
    if a is None:
        return False
    envelope = set(ev.BaseEvent.model_fields)
    return a.model_dump(exclude=envelope) == b.model_dump(exclude=envelope)


def replay_file(path: Path, *, as_json: bool = False, since: int = 1) -> None:
    """Render a recorded run, as it looked when it ran. Raises `OSError` when the file
    cannot be read; nothing inside it is a reason to refuse.

    The same renderer the live path uses, never a summary: a replay that abbreviated
    would be a second answer to keep in step with the first. A line this build cannot
    read — a tail cut off by an interrupt, a type invented by a newer build — renders
    as the unknown-event line, because losing everything already received is the worse
    failure. What is wrong with the file as a whole is said once, on stderr, after it.
    """
    # `errors="replace"` for the same reason a bad line is not fatal: an interrupt can
    # cut a file mid-character, and that is not a reason to refuse the rest.
    text = path.read_text(encoding="utf-8", errors="replace")

    # `since` counts physical lines, not `sequence`: sequence is monotonic within an
    # operation and restarts, so two runs recorded to one file — or an `ask` inside a
    # `run` — give it several line 3s. A line number is what a person reading the file
    # in an editor already has in the gutter.
    newest = ev.SCHEMA_VERSION
    headed: bool | None = None
    foreign: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        # Every line is read, even before `since`: whether the file opens with a
        # header is about the file, not about where this replay starts.
        event = ev.parse_event(line)
        if headed is None:
            headed = isinstance(event, ev.RunContext)
        newest = max(newest, event.schema_version)
        if isinstance(event, ev.RunContext):
            if event.format == ev.RECORDING_FORMAT:
                newest = max(newest, event.format_version)
            else:
                # Another format's version means nothing against ours.
                foreign.add(event.format)
        if number < since:
            continue
        if as_json:
            # The original line, byte for byte. Re-dumping the parsed event would
            # reorder fields and rewrite timestamps, so `replay --json` would not agree
            # with the `--json` run that produced the file.
            print(line)
        elif isinstance(event, ev.UserMessage):
            # The live renderer drops this one, because the frontend that caused it
            # has already echoed it. On replay nothing has, so the question would be
            # missing from the answer.
            console.print(f"[prompt]›[/] {escape(event.text)}")
        else:
            render_module.render(console, event)

    # Named once rather than per line, and after the render rather than instead of it:
    # the stream still reads, and saying nothing is what would let it be misread.
    if headed is False:
        err_console.print(
            "[warn]![/] [dim]no run context on the first line — a --json stream, or "
            "recorded before recordings had a header[/]"
        )
    for name in sorted(foreign):
        err_console.print(
            f"[warn]![/] [dim]a header names format '{escape(name)}', not "
            f"{ev.RECORDING_FORMAT}[/]"
        )
    if newest > ev.SCHEMA_VERSION:
        err_console.print(
            f"[warn]![/] [dim]recorded at schema_version {newest}; this build reads "
            f"{ev.SCHEMA_VERSION} — some lines may be misread.[/]"
        )


async def stream(
    session: Session,
    command: Command,
    *,
    as_json: bool = False,
    tee: Callable[[ev.BaseEvent], None] | None = None,
) -> bool:
    """Run one command, rendering or dumping as it goes. True when nothing failed.

    `tee` — from `recording` — receives every event as well, so `--record` adds a file
    without taking the live view away.
    """
    ok = True
    async for event in session.run(command):
        if isinstance(event, ev.AgentFailed):
            ok = False
        if as_json:
            print(ev.dump_event(event))
        else:
            render_module.render(console, event)
        if tee is not None:
            tee(event)
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
