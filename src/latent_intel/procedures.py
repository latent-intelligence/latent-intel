"""Declarative commands — a saved workflow, as a markdown file.

A **capability** needs code and arrives as a Python entry point: a connector says
where context comes from, a runtime says who answers. A **composition** needs no code,
and this is where it lives — a saved search, a prompt template, a short sequence of
both, authored by whoever owns the deployment.

The test between them is mechanical, and `compile()`'s return type is what makes it
structural rather than a convention: **if it can be expressed as `list[Command]` over
installed capabilities, it is a composition.** A procedure that wanted to do anything
else would have to change that signature, which is the argument happening in the open.

**What a procedure deliberately cannot do**, stated here because a reader will ask:

- run a shell command or spawn a process
- read or write a file
- call a connector tool directly — tools go through the router and its approval gate
- branch, loop, or bind more than one argument
- see step N's result in step N+1 — a `sequence` is a batch, not a pipeline

The last is the constraint most likely to chafe. *Reverse it* when a real workflow
needs a search's output inside a prompt — that is also when `context/` earns its
retrieval and assembly modules, and a step grows a `context: previous` field. Until
then, chaining is what `ask` over an attached source already does, better.

This module may not import `connectors`, `agent` or `registry` — a frontend needs the
command roster before a session exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .commands import Ask, Command, Find

#: What a procedure may be. Three, and a fourth needs an argument, not an if-branch.
KINDS = ("search", "prompt", "sequence")

#: Two placeholders, and no template language. A procedure is a saved workflow, not a
#: program, and every conditional added here is one the engine has to keep working.
_ARGUMENT = "{{argument}}"
_PROJECT = "{{project}}"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class Procedure:
    """One command a project offers."""

    name: str
    kind: str
    description: str = ""
    argument: str = ""
    argument_required: bool = False
    body: str = ""
    query: str = ""
    source: str | None = None
    limit: int = 10
    steps: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def usage(self) -> str:
        if not self.argument:
            return f"/{self.name}"
        return f"/{self.name} <{self.argument}>"


def parse(text: str, name: str) -> Procedure:
    """One file, as a procedure. Raises `ProcedureError` on anything unusable."""
    match = _FRONTMATTER.match(text)
    if match is None:
        raise ProcedureError(f"{name}: no frontmatter — a procedure starts with `---`")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ProcedureError(f"{name}: {exc}") from exc
    if not isinstance(meta, dict):
        raise ProcedureError(f"{name}: frontmatter must be a mapping")

    kind = str(meta.get("kind") or "").strip()
    if kind not in KINDS:
        raise ProcedureError(
            f"{name}: kind must be one of {', '.join(KINDS)}, got {kind or 'nothing'}"
        )

    procedure = Procedure(
        name=str(meta.get("name") or name),
        kind=kind,
        description=str(meta.get("description") or "").strip(),
        argument=str(meta.get("argument") or "").strip(),
        argument_required=bool(meta.get("argument_required")),
        body=match.group(2).strip(),
        query=str(meta.get("query") or "").strip(),
        source=str(meta["source"]) if meta.get("source") else None,
        limit=int(meta.get("limit") or 10),
        steps=[str(s) for s in (meta.get("steps") or [])],
    )

    if kind == "search" and not procedure.query:
        raise ProcedureError(f"{name}: a search needs a `query`")
    if kind == "prompt" and not procedure.body:
        raise ProcedureError(f"{name}: a prompt needs a body")
    if kind == "sequence" and not procedure.steps:
        raise ProcedureError(f"{name}: a sequence needs `steps`")
    return procedure


def discover(directory: Path | None) -> tuple[dict[str, Procedure], list[str]]:
    """Every procedure in a project's command directory, plus what could not be read.

    A file that fails to parse is reported and skipped, never fatal — the same rule
    `connectors.available_kinds()` applies to a broken plugin, and for the same reason:
    one bad file must not take the tool down.
    """
    found: dict[str, Procedure] = {}
    problems: list[str] = []
    if directory is None or not directory.is_dir():
        return found, problems

    for path in sorted(directory.glob("*.md")):
        try:
            procedure = parse(path.read_text(encoding="utf-8"), path.stem)
        except (ProcedureError, OSError) as exc:
            problems.append(str(exc))
            continue
        if procedure.name in found:
            problems.append(f"{procedure.name}: already defined, {path} skipped")
            continue
        procedure.path = path
        found[procedure.name] = procedure
    return found, problems


def substitute(text: str, argument: str, project: str) -> str:
    return text.replace(_ARGUMENT, argument).replace(_PROJECT, project)


def compile(  # noqa: A001 — it compiles; the shadowed builtin is not used here
    procedure: Procedure,
    argument: str = "",
    *,
    project: str = "",
    among: dict[str, Procedure] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> list[Command]:
    """A procedure and its argument, as commands the session already understands.

    Returning `list[Command]` is the whole boundary: a procedure cannot express anything
    the session could not already be asked to do, so no new Session surface and no new
    event type is needed for any of this.
    """
    if procedure.argument_required and not argument.strip():
        raise ProcedureError(
            f"{procedure.name} needs {procedure.argument or 'an argument'}"
        )

    if procedure.kind == "search":
        return [
            Find(
                query=substitute(procedure.query, argument, project),
                source=procedure.source,
                limit=procedure.limit,
            )
        ]
    if procedure.kind == "prompt":
        return [Ask(prompt=substitute(procedure.body, argument, project))]

    # sequence
    if procedure.name in _seen:
        raise ProcedureError(f"{procedure.name}: a sequence cannot include itself")
    catalogue = among or {}
    commands: list[Command] = []
    for step in procedure.steps:
        target = catalogue.get(step)
        if target is None:
            raise ProcedureError(f"{procedure.name}: no step named '{step}'")
        commands += compile(
            target,
            argument,
            project=project,
            among=catalogue,
            _seen=_seen | {procedure.name},
        )
    return commands


class ProcedureError(Exception):
    """A procedure file that cannot be used. Reported, never fatal."""
