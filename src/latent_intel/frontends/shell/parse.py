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
from dataclasses import dataclass

from ...commands import Ask, Command, Connect, Disconnect, Fetch, Find, ListSources


@dataclass
class Local:
    """Something the shell does itself: help, quitting, choosing a source."""

    action: str
    argument: str = ""


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
    "project": Spec(0, 1, "/project [name]", "which deployment is active", "agent"),
    "runtime": Spec(0, 1, "/runtime [name]", "which backend answers `ask`", "agent"),
    "model": Spec(0, 1, "/model [name]", "which model that backend uses", "agent"),
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


def _slash(text: str, procedures: dict[str, object] | None = None) -> Parsed:
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        return Invalid(f"could not read that line: {exc}")
    if not parts:
        return Invalid("no command after /", "try /help")

    name = ALIASES.get(parts[0], parts[0])
    spec = SLASH.get(parts[0]) or SLASH.get(name)
    if spec is None:
        # A project's own commands are looked up after the built-ins, never before: the
        # engine's grammar is identical on every deployment, and a project cannot
        # shadow `/exit`.
        if procedures is not None and parts[0] in procedures:
            return Procedure(parts[0], " ".join(parts[1:]))
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
        case (
            "exit"
            | "help"
            | "clear"
            | "tools"
            | "use"
            | "runtime"
            | "model"
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
