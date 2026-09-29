"""Subagents — a project's delegates, read from `agents/<name>.md`.

Claude Code's file format, so one deploy folder serves this engine, Claude Code and the
Agent SDK alike: frontmatter with `name` and `description`, optionally `tools` and
`model`, and the body as the subagent's prompt. A persona is the main agent's standing
instructions; a subagent is a delegate with its own context, which is the only thing an
agent buys that a skill does not.

**Composition, not capability.** Reading the files is here, beside `procedures.py`, and
a runtime that can run delegates says so by implementing `agent.base.Delegating`. A
runtime that cannot is reported by `doctor` rather than left to ignore the key — a
project that declares delegates and silently gets none is answering questions it
believes it handed off.

**Tools are named as `/tools` prints them**, `source.tool`, or by a tool the runtime
itself supplies. What a name resolves to is the runtime's to decide, because only it
knows what it offers; an entry that resolves to nothing is dropped from that turn and
reported by `doctor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import procedures

#: The keys this build reads. Claude Code reads more, and a file written for it will
#: carry them; each is reported as ignored rather than honoured by accident.
KEYS = frozenset({"name", "description", "tools", "disallowedTools", "model"})

#: Keys that could only narrow what a subagent may do, and that this build cannot
#: honour. Ignoring one would hand the delegate more than its file allows, so a file
#: that sets one is refused instead: the runtime's own permission policy decides.
REFUSED = frozenset({"permissionMode"})


@dataclass(frozen=True)
class Subagent:
    """One delegate, as a project declared it."""

    name: str
    description: str
    #: The subagent's own prompt — the body of its file.
    prompt: str
    #: What it may use, as named in the file. `None` inherits every tool the main
    #: agent has; an empty tuple is none at all.
    tools: tuple[str, ...] | None = None
    #: What it may not use, however `tools` reads — Claude Code's `disallowedTools`.
    disallowed: tuple[str, ...] = ()
    model: str | None = None


def _tools(value: str | list[object] | None) -> tuple[str, ...] | None:
    """`tools:` as a YAML list or Claude Code's comma-separated string."""
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else value
    return tuple(str(item).strip() for item in items if str(item).strip())


def load(directory: Path | None, problems: list[str]) -> list[Subagent]:
    """Every `*.md` in a project's agents directory, in filename order.

    One unreadable or incomplete file is reported and skipped rather than costing a
    deployment its other delegates. Frontmatter is required: a subagent without a
    `description` gives the main agent nothing to decide by.
    """
    found: list[Subagent] = []
    if directory is None:
        return found
    seen: set[str] = set()
    for path in sorted(directory.glob("*.md")):
        where = f"agents: {path.name}"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            # The file name, never the path: an unreadable file on a synced drive would
            # print the account's address in output meant to be safe to paste.
            problems.append(f"{where}: {exc.strerror or type(exc).__name__}")
            continue
        except UnicodeDecodeError:
            problems.append(f"{where}: not UTF-8")
            continue
        block, body = procedures.split_frontmatter(text)
        if block is None:
            problems.append(f"{where}: no frontmatter — a subagent starts with `---`")
            continue
        try:
            meta = yaml.safe_load(block) or {}
        except yaml.YAMLError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if not isinstance(meta, dict):
            problems.append(f"{where}: frontmatter must be a mapping")
            continue
        name = str(meta.get("name") or "").strip()
        description = " ".join(str(meta.get("description") or "").split())
        prompt = body.strip()
        missing = [
            key
            for key, value in (
                ("name", name),
                ("description", description),
                ("a body", prompt),
            )
            if not value
        ]
        if missing:
            problems.append(f"{where}: needs {', '.join(missing)} — skipped")
            continue
        refused = sorted(set(meta) & REFUSED)
        if refused:
            problems.append(
                f"{where}: `{refused[0]}` cannot be honoured here — the runtime's own "
                "permission policy decides; remove it — skipped"
            )
            continue
        tools, disallowed = meta.get("tools"), meta.get("disallowedTools")
        if any(
            v is not None and not isinstance(v, str | list) for v in (tools, disallowed)
        ):
            problems.append(
                f"{where}: `tools` and `disallowedTools` must be lists or "
                "comma-separated strings — skipped"
            )
            continue
        if name in seen:
            problems.append(f"{where}: '{name}' is declared twice — the first is kept")
            continue
        seen.add(name)
        for key in sorted(set(meta) - KEYS):
            problems.append(f"{where}: `{key}` is not read by this build — ignored")
        model = meta.get("model")
        found.append(
            Subagent(
                name=name,
                description=description,
                prompt=prompt,
                tools=_tools(tools),
                disallowed=_tools(disallowed) or (),
                model=str(model) if model else None,
            )
        )
    return found


__all__ = ["KEYS", "REFUSED", "Subagent", "load"]
