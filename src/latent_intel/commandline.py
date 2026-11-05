"""A command line, as a source's `target:` or a runtime's `command:` holds one.

Read the same way on every OS, so one deployment file serves a laptop and a Windows
machine: POSIX quoting, as `shlex` reads it, except that `\\` is an ordinary character.
`shlex` read each backslash in a Windows path as an escape and dropped it. `shlex.join`
never writes one, so a line it wrote reads back as it was.

`~` and `${VAR}` are expanded in each argument after the split, never in the line
before it, so a value holding a `\\` or a space arrives as one argument, intact.

A leaf module: a project reads it before any session exists.
"""

from __future__ import annotations

import os
import shlex


def split(line: str) -> list[str]:
    """The arguments in `line`, quotes removed. Raises `ValueError` on an open quote."""
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def argv(line: str) -> list[str]:
    """`split`, then `expand` on each argument."""
    return [expand(part) for part in split(line)]


def expand(value: str) -> str:
    """`~` and `${VAR}`, but leave a URI alone.

    Not through `Path`, which writes `\\` on Windows: a caller that records or splits
    the result gets what was typed, with only the expansions changed.
    """
    if "://" in value:
        return value
    return os.path.expanduser(os.path.expandvars(value))
