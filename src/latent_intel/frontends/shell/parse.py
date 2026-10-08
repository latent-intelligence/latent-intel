"""Turning a typed line into a `Command`, or into something the shell handles itself.

Split out from the loop because it is the only part worth testing exhaustively, and
testing it should not need a terminal.

The grammar is one rule: **a leading `/` means session management, anything else is a
query.** That split comes from what the two kinds of input actually are. `/connect` and
`/use` change what the session *is*; `search` and `open` ask it something. Making them
look different means a typo in one can never be silently read as the other — and it is
the shape people already know from every agent shell they have used.
"""

from __future__ import annotations

import difflib
import shlex
from dataclasses import dataclass, field

from ...commands import Ask, Command, Connect, Disconnect, Fetch, Find, ListSources
from ...events import PURPOSES
from ...models import WEB_MODES


@dataclass
class Local:
    """Something the shell does itself: help, quitting, choosing a source."""

    action: str
    argument: str = ""
    #: `--flag value` pairs, for the commands that take any — keyed without the dashes.
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class Invalid:
    """A line that did not parse, with what to type instead."""

    message: str
    hint: str = ""


@dataclass(frozen=True)
class Procedure:
    """A project's own command. Held as a name and its argument — the engine resolves it
    against the active project's catalogue at dispatch, not at parse."""

    name: str
    argument: str = ""


Parsed = Command | Local | Invalid | Procedure


#: `/`-commands, each carrying everything about itself — arity, usage, summary and
#: group. Help and completion are generated from here, so a command cannot be added to
#: the grammar and forgotten in the documentation. Checked before dispatch so a
#: mistyped command says what it wanted rather than failing somewhere deeper.
@dataclass(frozen=True)
class Spec:
    """One command, in one place.

    Arity, usage, summary and group used to live across four structures — this table,
    the routing match below, the dispatch match in `repl`, and a hand-written `HELP`
    string. Help and completion are now generated from here, so a command cannot be
    added to the grammar and forgotten in the documentation, which had already happened
    (`exit` was missing from completion).
    """

    min_args: int
    max_args: int
    usage: str
    summary: str = ""
    group: str = "session"


SLASH: dict[str, Spec] = {
    "sources": Spec(0, 0, "/sources", "what is attached"),
    "connect": Spec(
        # 6, not 3: flags and their values are separate tokens here, so `<store> --kind
        # k --as n --remote` is six. Counting them as three rejected the documented use.
        1,
        6,
        "/connect <store> [--kind <k>] [--as <id>] [--remote]",
        "attach one — a registered id, or a path with --kind",
    ),
    "disconnect": Spec(1, 1, "/disconnect <source>", "detach one"),
    "use": Spec(0, 1, "/use [source]", "which source a bare key resolves against"),
    "tools": Spec(0, 0, "/tools", "what the router exposes"),
    "record": Spec(
        # 5: a file, then `--purpose p` and `--title t`, each flag and value a token.
        0,
        5,
        '/record [file | off] [--purpose share|demo|eval] [--title "…"]',
        "tee every event to a file, headed by what produced it",
    ),
    "replay": Spec(1, 3, "/replay <file> [--since N]", "render a recording"),
    "project": Spec(0, 1, "/project [name]", "which deployment is active", "agent"),
    "runtime": Spec(0, 1, "/runtime [name]", "which backend answers `ask`", "agent"),
    "model": Spec(0, 1, "/model [name]", "which model that backend uses", "agent"),
    "host": Spec(0, 1, "/host [name]", "which host the runtime talks to", "agent"),
    "hosts": Spec(0, 0, "/hosts", "every host, and what each one needs", "agent"),
    "scope": Spec(
        # 3: a mode, then `--allow` or `--block` and its list, each a token.
        0,
        3,
        "/scope [off|search|browse] [--allow a,b | --block a,b | --any]",
        "how much of the web the agent may reach — this session only",
        "agent",
    ),
    "clear": Spec(0, 0, "/clear", "clear the screen"),
    "help": Spec(0, 1, "/help [command]", "this"),
    "exit": Spec(0, 0, "/exit", "leave"),
    "quit": Spec(0, 0, "/quit", "leave"),
}

#: Bare-word verbs. Everything else is treated as a question for the agent.
VERBS = ("search", "open", "get", "ask")

ALIASES = {"quit": "exit", "get": "open"}


def parse(
    line: str, *, limit: int = 10, procedures: dict[str, object] | None = None
) -> Parsed | None:
    """Read one line. `None` means it was blank and nothing should happen.

    `procedures` is the active project's command catalogue. Passing it here rather than
    importing it keeps this module pure and exhaustively testable with a fabricated map,
    which is the entire reason it exists as a separate file.
    """
    text = line.strip()
    if not text:
        return None
    if text.startswith("/"):
        return _slash(text[1:], procedures)
    return _bare(text, limit)


def builtin(name: str) -> bool:
    """Whether `/name` is the shell's own. `get` is an alias of the bare verb `open`,
    not of a `/` command, so it is not."""
    return name in SLASH or ALIASES.get(name) in SLASH


def _slash(text: str, procedures: dict[str, object] | None = None) -> Parsed:
    # A project's own commands and skills are looked up after the built-ins, never
    # before: the engine's grammar is identical on every deployment, and a project
    # cannot shadow `/exit`. Their argument is the raw rest of the line — it is a
    # question, and `what's` is not an unclosed quote.
    head, _, rest = text.strip().partition(" ")
    if procedures is not None and head in procedures and not builtin(head):
        return Procedure(head, rest.strip())
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        return Invalid(f"could not read that line: {exc}")
    if not parts:
        return Invalid("no command after /", "try /help")

    name = ALIASES.get(parts[0], parts[0])
    spec = SLASH.get(parts[0]) or SLASH.get(name)
    if spec is None:
        close = [c for c in SLASH if c.startswith(parts[0][:2])]
        return Invalid(
            f"no command /{parts[0]}",
            f"did you mean /{close[0]}?" if close else "try /help",
        )

    args = parts[1:]
    if not spec.min_args <= len(args) <= spec.max_args:
        return Invalid(
            f"/{parts[0]} takes {spec.min_args}–{spec.max_args} argument(s)", spec.usage
        )

    match name:
        case "record":
            return _record(args)
        case "replay":
            return _replay(args)
        case "scope":
            return _scope(args)
        case (
            "exit"
            | "help"
            | "clear"
            | "tools"
            | "use"
            | "runtime"
            | "model"
            | "host"
            | "hosts"
            | ("project")
        ):
            return Local(name, args[0] if args else "")
        case "sources":
            return ListSources()
        case "disconnect":
            return Disconnect(source_id=args[0])
        case "connect":
            return _connect(args)
    return Invalid(f"no command /{parts[0]}", "try /help")


def _flags(args: list[str], allowed: set[str]) -> tuple[list[str], dict[str, str]]:
    """Split positional arguments from `--flag value` pairs. Raises `ValueError` with
    the message to show for a flag that is unknown or has no value."""
    positional: list[str] = []
    options: dict[str, str] = {}
    rest = list(args)
    while rest:
        token = rest.pop(0)
        if not token.startswith("--"):
            positional.append(token)
            continue
        name = token[2:]
        if name not in allowed:
            raise ValueError(f"unknown option {token}")
        if not rest:
            raise ValueError(f"{token} needs a value")
        options[name] = rest.pop(0)
    return positional, options


def _record(args: list[str]) -> Parsed:
    usage = SLASH["record"].usage
    try:
        positional, options = _flags(args, {"purpose", "title"})
    except ValueError as exc:
        return Invalid(str(exc), usage)
    if len(positional) > 1:
        return Invalid("/record takes one file", usage)
    target = positional[0] if positional else ""
    if options and target in {"", "off", "none"}:
        return Invalid("--purpose and --title describe a new recording", usage)
    purpose = options.get("purpose")
    if purpose is not None and purpose not in PURPOSES:
        return Invalid(f"no purpose '{purpose}'", f"one of {', '.join(PURPOSES)}")
    return Local("record", target, options)


def _replay(args: list[str]) -> Parsed:
    usage = SLASH["replay"].usage
    try:
        positional, options = _flags(args, {"since"})
    except ValueError as exc:
        return Invalid(str(exc), usage)
    if len(positional) != 1:
        return Invalid("/replay takes one file", usage)
    since = options.get("since", "1")
    if not since.isdigit() or int(since) < 1:
        return Invalid(f"--since takes a line number, not '{since}'", usage)
    return Local("replay", positional[0], options)


def _scope(args: list[str]) -> Parsed:
    """`/scope [mode] [--allow a,b | --block a,b | --any]`. A list replaces the one in
    force; `--any` drops both lists and `max_uses`; with neither, what is configured is
    kept. `--any` takes no value, so it is lifted out before the flags are read."""
    usage = SLASH["scope"].usage
    bare = [arg for arg in args if arg != "--any"]
    try:
        positional, options = _flags(bare, {"allow", "block"})
    except ValueError as exc:
        return Invalid(str(exc), usage)
    if len(bare) != len(args):
        options["any"] = ""
    if len(positional) > 1:
        return Invalid("/scope takes one mode", usage)
    mode = positional[0] if positional else ""
    if options and not mode:
        return Invalid("--allow, --block and --any need a mode to apply to", usage)
    if len(options) > 1:
        return Invalid("one of --allow, --block or --any", usage)
    if mode and mode not in WEB_MODES:
        return Invalid(f"no mode '{mode}'", f"one of {', '.join(WEB_MODES)}")
    return Local("scope", mode, options)


def _connect(args: list[str]) -> Parsed:
    """`/connect <store> [--kind k] [--as name]`, parsed by hand.

    Hand-rolled rather than argparse because argparse exits the process on a bad flag,
    which in a REPL kills the session someone has spent ten minutes assembling.
    """
    spec = args[0]
    kind: str | None = None
    name: str | None = None
    remote = False
    rest = args[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--remote":
            remote = True
            continue
        if not rest:
            return Invalid(f"{flag} needs a value", "/connect <store> --kind <kind>")
        value = rest.pop(0)
        if flag in ("--kind", "-k"):
            kind = value
        elif flag in ("--as", "-a"):
            name = value
        else:
            return Invalid(f"unknown option {flag}", "/connect <store> --kind <kind>")
    return Connect(spec=spec, kind=kind, source_id=name, remote=remote)


def _bare(text: str, limit: int) -> Parsed:
    head, _, rest = text.partition(" ")
    verb = ALIASES.get(head, head)
    rest = rest.strip()

    if head in VERBS or verb in VERBS:
        if not rest:
            return Invalid(f"{head} needs something to act on", f"{head} <query>")
        if verb == "search":
            return Find(query=rest, limit=limit)
        if verb == "open":
            return Fetch(ref=rest)
        return Ask(prompt=rest)

    # A near-miss on a verb is far more likely to be a typo than a question that happens
    # to start with a similar word. Suggest rather than redirect: silently rewriting
    # someone's input is worse than asking, and the hint says how to force the question
    # through if the guess was wrong.
    if rest and (close := difflib.get_close_matches(head, VERBS, n=1, cutoff=0.8)):
        return Invalid(
            f"did you mean `{close[0]} {rest}`?",
            f"or to ask it as a question: ask {text}",
        )

    # Anything else is a question. The alternative — refusing unknown input — would make
    # the shell a worse CLI rather than a better one.
    return Ask(prompt=text)
