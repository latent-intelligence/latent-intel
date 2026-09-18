"""The interactive shell — the primary experience.

A keyboard-first AI shell, not a small web app. Inline streaming, tool activity,
approvals, citations, slash commands and **ordinary terminal scrollback**: no alternate
screen, no persistent panes, no redraw. You can scroll back through a session, select
text with the mouse, and pipe the transcript somewhere. Panes and readers are the web
client's job, and building them twice is what this split avoids.

Two details that matter more than they look:

**`patch_stdout` wraps the prompt.** Without it, anything printed while the prompt is
live corrupts the line being edited. With it, streamed output and a live input line
coexist — which is the whole reason this is `prompt_toolkit` rather than `input()`.

**History persists, in the data directory.** A shell you have to retype into is a shell
people stop using. It goes under `~/.local/share`, never beside the configuration, and
never inside the install directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.markup import escape

from ... import __version__
from ... import config as config_module
from ... import events as ev
from ... import procedures as procedures_module
from ... import settings as settings_module
from ...commands import Command, Connect
from ...models import Descriptor, RuntimeUnavailable
from ...session import Session
from ...ui import banner
from ...ui import render as render_module
from .._shared import (
    active_brand,
    console,
    print_hosts,
    record,
    report,
    session_scope,
)
from .._shared import recording as open_recording
from .parse import SLASH, Invalid, Local, Procedure, parse


@dataclass
class Recording:
    """Where this session is teeing its events, and how many have gone there.

    Session state and nothing else: a recording is about this run, not about this
    machine, so it is never written to config — a shell that remembered one would
    quietly append tomorrow's session to yesterday's file.
    """

    path: Path | None = None
    count: int = 0


def _prompt_style() -> Style:
    """The `prompt` token as prompt_toolkit wants it, resolved when the shell starts.

    Read off the console rather than the module's default palette, and at call time
    rather than at import. This was a module-level constant built from `THEME`, which
    made it the one thing a project could not retheme: `use_brand` pushes the merged
    theme onto the console's own stack and leaves `THEME` alone, so a `prompt:` override
    changed every other token and never this one. The console is where the resolved
    theme actually lives, so asking it keeps the single palette the helper promised.
    """
    return Style.from_dict({"prompt": str(console.get_style("prompt"))})


def help_text(procedures: dict[str, Any] | None = None) -> str:
    """The help screen, generated from the command table and the active project.

    Written by hand until the table carried summaries, which meant every new command had
    to be documented in a second place and eventually was not. A project's own commands
    appear here too — a command nobody can discover may as well not exist.
    """
    groups: dict[str, list[str]] = {}
    for name, spec in SLASH.items():
        if name == "quit":  # an alias for /exit; listing both is noise
            continue
        groups.setdefault(spec.group, []).append(
            f"  [tool]/{name}[/]{' ' * max(1, 14 - len(name))}{escape(spec.summary)}"
        )

    lines = ["[accent.strong]session[/]", *groups.get("session", [])]
    lines += ["", "[accent.strong]agent[/]", *groups.get("agent", [])]
    lines += [
        "",
        "[accent.strong]asking[/]",
        "  [tool]search[/] <query>   every attached source, grouped — never merged",
        "  [tool]open[/] <ref>       one document in full",
        "  [tool]ask[/] <question>   the agent",
    ]
    if procedures:
        lines += ["", "[accent.strong]this project[/]"]
        for name, procedure in sorted(procedures.items()):
            summary = escape(getattr(procedure, "description", ""))
            lines.append(f"  [tool]/{name}[/]{' ' * max(1, 14 - len(name))}{summary}")
    lines += [
        "",
        "Anything not starting with [tool]/[/] and not a verb above is a question.",
    ]
    return "\n".join(lines)


# Bracketed placeholders are escaped (`\[id]`) because rich reads `[id]` as a style tag
# and silently drops it — the help text lost its own argument name for a week.


class ShellCompleter(Completer):
    """Completes commands, source ids and page keys.

    Keys come from the last search rather than from the whole store: completing over
    every key in a 294-page wiki is a list nobody reads, while the six things you just
    looked at is the list you actually want.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.recent: list[str] = []

    def get_completions(self, document: Document, complete_event: object):  # type: ignore[no-untyped-def]
        text = document.text_before_cursor
        word = document.get_word_before_cursor(WORD=True)

        if text.startswith("/") and " " not in text:
            for name in _SLASH_NAMES:
                if name.startswith(text[1:]):
                    yield Completion(f"/{name}", start_position=-len(text))
            return

        if text.startswith(("/connect", "/disconnect", "/use", "/source")):
            for source in self.session.sources():
                if source.id.startswith(word):
                    yield Completion(source.id, start_position=-len(word))
            return

        if text.startswith(("open ", "get ")):
            for ref in self.recent:
                if ref.startswith(word):
                    yield Completion(ref, start_position=-len(word))
            return

        if not text or " " not in text:
            for verb in ("search ", "open ", "ask "):
                if verb.startswith(text):
                    yield Completion(verb, start_position=-len(text))


#: Derived, never hand-listed: the literal this replaced had drifted from `SLASH` and
#: was already missing `exit`, so `/ex<Tab>` completed nothing.
_SLASH_NAMES = tuple(sorted(SLASH))


async def run() -> None:
    """Open the shell. Returns when the user leaves."""
    async with session_scope() as (session, problems):
        await _loop(session, problems)
    console.print("[dim]bye[/]")


async def _loop(session: Session, problems: list[str]) -> None:
    capabilities, scale = banner.summarise(session.sources())
    resolved = settings_module.load()
    project_name = resolved.project_name or ""
    catalogue, command_problems = procedures_module.discover(
        resolved.project.commands_dir if resolved.project else None
    )
    brand = active_brand()
    banner.render(
        console,
        __version__,
        capabilities,
        scale,
        hints=("/help", "/connect", "/sources", "/exit"),
        brand=brand,
    )
    report(problems + command_problems)
    console.print()

    completer = ShellCompleter(session)
    recording = Recording()
    prompt: PromptSession[str] = PromptSession(
        history=FileHistory(str(_history_path())),
        completer=completer,
        complete_while_typing=False,
        style=_prompt_style(),
    )

    while True:
        try:
            with patch_stdout():
                line = await prompt.prompt_async(brand.prompt_text)
        except KeyboardInterrupt:
            # Ctrl-C abandons the line, never the session — the same contract every
            # other shell has. Ctrl-D is how you leave.
            continue
        except EOFError:
            break

        parsed = parse(line, procedures=dict(catalogue))
        if parsed is None:
            continue
        if isinstance(parsed, Invalid):
            console.print(f"[fail]✗[/] {escape(parsed.message)}")
            if parsed.hint:
                console.print(f"  [dim]{escape(parsed.hint)}[/]")
            continue
        if isinstance(parsed, Local):
            if _local(session, parsed, recording, catalogue):
                break
            continue
        if isinstance(parsed, Procedure):
            for command in _compile(catalogue, parsed, project_name):
                await _dispatch(session, command, completer, recording)
            continue
        await _dispatch(session, parsed, completer, recording)


def _compile(
    catalogue: dict[str, procedures_module.Procedure],
    parsed: Procedure,
    project: str,
) -> list[Command]:
    """A project command, as commands the session already understands.

    A bad procedure is reported and skipped. The alternative — a traceback out of a
    file the deployment wrote — makes the engine look broken for someone else's typo.
    """
    procedure = catalogue.get(parsed.name)
    if procedure is None:
        console.print(f"[fail]✗[/] no command /{escape(parsed.name)}")
        return []
    try:
        return procedures_module.compile(
            procedure, parsed.argument, project=project, among=catalogue
        )
    except procedures_module.ProcedureError as exc:
        console.print(f"[fail]✗[/] {escape(str(exc))}")
        return []


async def _dispatch(
    session: Session,
    command: Command,
    completer: ShellCompleter,
    recording: Recording,
) -> None:
    """Run one command, render it, and learn from what came back.

    Two side effects, both about the two frontends staying in step: refs from a search
    feed completion, and a successful attach is written to the same config the CLI
    reads — so attaching here and running `intel search` in another terminal cannot
    disagree about what is connected.

    The tee is the third, and it is `_shared.recording` — the one the CLI opens too, so
    a session's file and a `--record` file are written by the same three lines. This
    loop cannot call `stream` itself: the two side effects above need every event as it
    passes.
    """
    refs: list[str] = []
    with open_recording(recording.path) as tee:
        async for event in session.run(command):
            if isinstance(event, ev.RetrievalResult):
                refs.extend(hit.ref for hit in event.hits)
            # Guarded on the *command*, not the event: `/sources` also emits
            # SourceConnected for everything already attached, and treating that as an
            # attach would rewrite the config on every listing.
            elif isinstance(event, ev.SourceConnected) and isinstance(command, Connect):
                _persist(event.descriptor, command)
            elif isinstance(event, ev.SourceDisconnected):
                _forget(event.source_id)
            render_module.render(console, event)
            tee(event)
            if recording.path is not None:
                recording.count += 1
    if refs:
        completer.recent = refs
    console.print()


def _persist(descriptor: Descriptor, command: Connect) -> None:
    record(
        command.spec,
        descriptor,
        remote=command.remote,
        by_name=command.kind is None,
        options=command.options,
    )


def _forget(source_id: str) -> None:
    cfg = config_module.load()
    if cfg.remove(source_id):
        config_module.save(cfg)


def _local(
    session: Session,
    local: Local,
    recording: Recording,
    procedures: dict[str, Any] | None = None,
) -> bool:
    """Handle a shell-side command. True means leave."""
    match local.action:
        case "exit":
            return True
        case "clear":
            console.clear()
        case "help":
            console.print(help_text(procedures))
        case "tools":
            tools = session.tools()
            if not tools:
                console.print("[dim]no tools — nothing attached offers any[/]")
            for tool in tools:
                mark = "" if tool.effect_declared else "  [warn](effect undeclared)[/]"
                console.print(
                    f"  [tool]{tool.qualified:<28}[/] [dim]{tool.effect}[/]{mark}"
                )
                if tool.description:
                    console.print(f"    [dim]{escape(tool.description[:100])}[/]")
        case "record":
            _record(recording, local.argument)
        case "use":
            _use(session, local.argument)
        case "runtime":
            _runtime(session, local.argument)
        case "model":
            _model(session, local.argument)
        case "host":
            _host(session, local.argument)
        case "hosts":
            # The whole table, printed by the same function `intel hosts` calls: a
            # shell that phrased an endpoint's needs differently from the CLI would
            # be a second answer to keep in step with the first.
            print_hosts()
        case "project":
            _project(session, local.argument)
    return False


def _project(session: Session, name: str) -> None:
    """Report or switch the active project.

    Switching mid-session does not reattach — the sources of the project you left are
    still open, and silently swapping them under a running shell would be worse than
    saying so. Restart to pick up the new set.
    """
    resolved = settings_module.load()
    if not name:
        console.print(f"[dim]project[/] [source]{resolved.project_name or 'none'}[/]")
        return

    config = config_module.load()
    config.project = None if name in {"none", "off"} else name
    config_module.save(config)
    settings_module.invalidate()
    console.print(f"[dim]project[/] [source]{escape(name)}[/]")
    console.print("[dim]restart the shell to attach its sources[/]")


def _record(recording: Recording, argument: str) -> None:
    """Start, stop, or report this session's recording.

    Nothing is persisted: `/record` changes what this shell does with its events, not
    what the machine is configured to do, so unlike `/connect` or `/runtime` it writes
    no config at all.

    A file the person chose and nothing beside it — no manifest, no index, no directory
    convention. Anything more is a store, and this is not the package that owns one.
    """
    if not argument:
        if recording.path is None:
            console.print("[dim]not recording[/] [dim]— /record <file> to start[/]")
            return
        console.print(
            f"[dim]recording to[/] [source]{escape(str(recording.path))}[/] "
            f"[dim]({recording.count} events)[/]"
        )
        return

    if argument in {"off", "none"}:
        if recording.path is None:
            console.print("[dim]not recording[/]")
            return
        console.print(
            f"[dim]stopped —[/] {recording.count} [dim]events written to[/] "
            f"[source]{escape(str(recording.path))}[/]"
        )
        recording.path = None
        recording.count = 0
        return

    path = Path(argument).expanduser()
    try:
        # Opened now rather than at the next command: a mistyped directory is a
        # diagnosis here and a traceback out of the loop there, which would take the
        # session with it.
        path.open("a", encoding="utf-8").close()
    except OSError as exc:
        console.print(f"[fail]✗[/] {escape(str(exc))}")
        return
    recording.path = path
    recording.count = 0
    console.print(
        f"[dim]recording to[/] [source]{escape(str(path))}[/] "
        "[dim]— /record off to stop[/]"
    )


def _use(session: Session, source_id: str) -> None:
    if not source_id:
        current = session.current or "none"
        console.print(f"[dim]bare keys resolve against[/] [source]{current}[/]")
        return
    if source_id not in {s.id for s in session.sources()}:
        console.print(f"[fail]✗[/] '{escape(source_id)}' is not attached")
        return
    session.current = source_id
    console.print(f"[dim]bare keys now resolve against[/] [source]{source_id}[/]")


def _runtime(session: Session, name: str) -> None:
    """Choose which backend answers `ask`, and remember it.

    Persists immediately and without confirmation, which matches `/connect` and
    `/disconnect` — both already write config on success. Asking here would be the
    inconsistent choice, not the careful one.
    """
    kinds = Session.runtime_status()
    installed = ", ".join(sorted(kinds)) or "none installed"

    if not name:
        # The resolved view, not the user's file: a project may name the runtime and
        # the model, and reading only config.yaml reported neither.
        resolved = settings_module.load()
        current = session.runtime_kind or resolved.runtime
        if not current:
            console.print("[dim]no runtime configured[/]")
            console.print(f"[dim]installed:[/] {escape(installed)}")
            return
        model = Session.runtime_setting(current, "model") or "unset"
        console.print(
            f"[dim]runtime[/] [source]{escape(current)}[/] [dim]({escape(model)})[/]"
        )
        console.print(f"[dim]installed:[/] {escape(installed)}")
        return

    if name in {"none", "off"}:
        session.set_runtime(None)
        settings = config_module.load()
        settings.runtime = None
        config_module.save(settings)
        console.print("[dim]no runtime configured[/]")
        return

    if name not in kinds:
        console.print(
            f"[fail]✗[/] no runtime '{escape(name)}' "
            f"[dim](installed: {escape(installed)})[/]"
        )
        return

    try:
        session.set_runtime(name)
    except RuntimeUnavailable as exc:
        # Building a runtime can now refuse — an option it does not know is rejected
        # rather than ignored — and a traceback in a REPL is not a diagnosis. Nothing
        # is persisted, because the config as written is what was refused.
        console.print(f"[fail]✗[/] {escape(str(exc))}")
        return
    settings = config_module.load()
    settings.runtime = name
    config_module.save(settings)
    console.print(f"[dim]runtime[/] [source]{escape(name)}[/]")
    # Set anyway: a machine can be configured before the binary is installed, and the
    # honest failure still arrives when someone asks. The reason comes with it, because
    # "cannot run here" does not say which variable to export — and it is the one this
    # handler already read, since choosing a runtime changes no runtime's own options.
    _report_reason(kinds[name])


def _model(session: Session, name: str) -> None:
    """Report or choose the model the configured runtime asks for."""
    _runtime_option(session, "model", name)


def _host(session: Session, name: str) -> None:
    """Report or choose which endpoint the configured runtime talks to.

    A host is a row in one runtime's table, so it means nothing without the runtime it
    belongs to. `/host none` clears your override rather than the setting: a host the
    project declares stays in force, and what is in force is what is printed.
    """
    _runtime_option(session, "host", name)


def _runtime_option(session: Session, key: str, name: str) -> None:
    """Report or set one option on the configured runtime, and remember it.

    `/model` and `/host` are one command with a different key: both name something that
    is meaningless without the runtime it belongs to, which is why config nests them
    under one and why both refuse when nothing is configured rather than storing a value
    with nowhere to live. Both have to put the option back when the rebuilt runtime
    refuses it, too — `claude-cli` has no hosts, and leaving `host:` under it would
    refuse every later `ask` on a runtime that answered a moment ago. `/model` wrote
    without the rollback, which is the divergence one handler ends.

    What is reported is the built runtime's own setting, never the file's and never the
    word that was typed. A runtime resolves its host across the user's file, the
    project's `agent:` block and `LATENT_INTEL_<RUNTIME>_HOST`; reading the file called
    a host set by the environment "its default", and `/host none` under a project that
    declares one reported `none` while every question still went to the declared host.
    """
    settings = config_module.load()
    resolved = settings_module.load()
    current = session.runtime_kind or resolved.runtime
    if not current:
        # The kinds installed, not one named as an example: `/runtime claude-cli` was
        # advice to install something on a machine that has the other two.
        installed = ", ".join(sorted(Session.runtime_status())) or "none"
        console.print(
            f"[fail]✗[/] no runtime configured "
            f"[dim]— /runtime <kind> first (installed: {escape(installed)})[/]"
        )
        return

    if not name:
        shown = Session.runtime_setting(current, key) or "unset"
        console.print(
            f"[dim]{key}[/] [source]{escape(shown)}[/] "
            f"[dim]for {escape(current)}[/]"
        )
        _report_reason(Session.runtime_reason(current))
        return

    before = settings.runtime_options(current).get(key)
    settings.set_runtime_option(current, key, None if name in {"none", "off"} else name)
    config_module.save(settings)
    try:
        session.set_runtime(current)  # rebuild with the new option
    except RuntimeUnavailable as exc:
        # Building a runtime can refuse — an option it does not know is rejected rather
        # than ignored — and a traceback in a REPL is not a diagnosis. The option goes
        # back as it was, so the refusal leaves nothing behind to be found later.
        settings.set_runtime_option(current, key, before)
        config_module.save(settings)
        console.print(f"[fail]✗[/] {escape(str(exc))}")
        return
    shown = Session.runtime_setting(current, key) or "unset"
    console.print(
        f"[dim]{key}[/] [source]{escape(shown)}[/] [dim]for {escape(current)}[/]"
    )
    _report_reason(Session.runtime_reason(current))


def _report_reason(reason: str | None) -> None:
    """The runtime's own reason, where it has one.

    Set anyway and report: the reason already names the known hosts when the name is a
    typo and the missing variables when it is not, and a machine is often pointed at a
    runtime, a model or a host before it is given the credentials for it. One printer
    for all three commands, because a reason phrased differently per command reads as a
    different kind of failure.

    The reason arrives rather than being fetched: `/runtime` has just read the whole
    status table and building every installed runtime a second time to print one line
    of it is work nobody asked for.
    """
    if reason is not None:
        console.print(f"[warn]![/] [dim]{escape(reason)}[/]")


def _history_path() -> Path:
    """History lives with data, never beside configuration."""
    directory = config_module.data_home()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "history"
