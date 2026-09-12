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
from ...models import Descriptor
from ...session import Session
from ...ui import banner
from ...ui import render as render_module
from ...ui import theme as theme_module
from .._shared import (
    active_brand,
    console,
    record,
    report,
    session_scope,
)
from .parse import SLASH, Invalid, Local, Procedure, parse

#: Matches the `prompt` token in the rich theme, so the input line and the output agree.
#: Derived from the rich theme rather than copied out of it. prompt_toolkit cannot read
#: a `Theme`, but it reads what a rich `Style` stringifies to — so there is one palette,
#: not two that drift.
PROMPT_STYLE = Style.from_dict({"prompt": theme_module.prompt_style()})


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
    prompt: PromptSession[str] = PromptSession(
        history=FileHistory(str(_history_path())),
        completer=completer,
        complete_while_typing=False,
        style=PROMPT_STYLE,
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
            if _local(session, parsed, catalogue):
                break
            continue
        if isinstance(parsed, Procedure):
            for command in _compile(catalogue, parsed, project_name):
                await _dispatch(session, command, completer)
            continue
        await _dispatch(session, parsed, completer)


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
    session: Session, command: Command, completer: ShellCompleter
) -> None:
    """Run one command, render it, and learn from what came back.

    Two side effects, both about the two frontends staying in step: refs from a search
    feed completion, and a successful attach is written to the same config the CLI
    reads — so attaching here and running `intel search` in another terminal cannot
    disagree about what is connected.
    """
    refs: list[str] = []
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
    if refs:
        completer.recent = refs
    console.print()


def _persist(descriptor: Descriptor, command: Connect) -> None:
    record(
        command.spec,
        descriptor,
        remote=command.remote,
        by_name=command.kind is None,
    )


def _forget(source_id: str) -> None:
    cfg = config_module.load()
    if cfg.remove(source_id):
        config_module.save(cfg)


def _local(
    session: Session, local: Local, procedures: dict[str, Any] | None = None
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
        case "use":
            _use(session, local.argument)
        case "runtime":
            _runtime(session, local.argument)
        case "model":
            _model(session, local.argument)
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
        model = resolved.model_for(current) or "its own default"
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

    session.set_runtime(name)
    settings = config_module.load()
    settings.runtime = name
    config_module.save(settings)
    console.print(f"[dim]runtime[/] [source]{escape(name)}[/]")
    if (reason := kinds[name]) is not None:
        # Set anyway: a machine can be configured before the binary is installed, and
        # the honest failure still arrives when someone asks. The reason comes with it,
        # because "cannot run here yet" does not say which variable to export.
        console.print(
            f"[warn]![/] [dim]{escape(name)} cannot run here yet — "
            f"{escape(reason)}[/]"
        )


def _model(session: Session, name: str) -> None:
    """Choose the model for the configured runtime.

    A model name is meaningless without the runtime it belongs to, which is why config
    nests it under one — so this refuses when nothing is configured rather than storing
    a value with nowhere to live.
    """
    settings = config_module.load()
    current = session.runtime_kind or settings_module.load().runtime
    if not current:
        console.print(
            "[fail]✗[/] no runtime configured [dim]— /runtime claude-cli first[/]"
        )
        return

    if not name:
        model = settings_module.load().model_for(current)
        if model:
            console.print(
                f"[dim]model[/] [source]{escape(model)}[/] "
                f"[dim]for {escape(current)}[/]"
            )
        else:
            console.print(
                f"[dim]no model set — {escape(current)} uses its own default[/]"
            )
        return

    settings.set_runtime_option(current, "model", name)
    config_module.save(settings)
    session.set_runtime(current)  # rebuild with the new option
    console.print(
        f"[dim]model[/] [source]{escape(name)}[/] [dim]for {escape(current)}[/]"
    )
    console.print(
        "[dim]an unknown name fails when you ask, with the runtime's own message[/]"
    )


def _history_path() -> Path:
    """History lives with data, never beside configuration."""
    directory = config_module.data_home()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "history"
