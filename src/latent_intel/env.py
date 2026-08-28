"""Credentials and locations from a `.env`, as an override on the ambient environment.

The default path stays untouched: S3 goes through `s3fs` to `botocore`, which reads
`aws configure`, `AWS_PROFILE`, SSO and instance roles on its own. This package has no
credential code and should never grow any. `.env` exists for the machine where none of
that is set up — a fresh laptop, a container, a colleague running a deployment for the
first time.

Two rules decide everything here.

**An exported variable always wins.** `.env` fills gaps; it never overrides a value the
process already had. That is the same precedence a project's `vars:` already follows
(`project.substitute`), so the package has one rule rather than two, and a deliberate
one-off — `AWS_PROFILE=other intel search …` — keeps working.

**Values never reach a command line.** A subprocess agent's MCP block is passed to
`claude` as an argument, so a credential placed there would be visible in `ps`. The
child is therefore told the *path* and loads the file itself, which is why `files()`
exists. It is also why MCP's own env whitelist — `HOME, LOGNAME, PATH, SHELL, TERM,
USER`, and nothing else — does not silently strip the credentials a served source needs.
"""

from __future__ import annotations

import os
from pathlib import Path

FILENAME = ".env"

#: Every file loaded this process, nearest-first. Recorded so a subprocess can be handed
#: the same paths; see `serve.launch_spec`.
_loaded: list[Path] = []


def candidates(project_dir: Path | None, home: Path | None) -> list[Path]:
    """Where a `.env` may live, highest precedence first.

    The project's own directory first, because credentials belong to the deployment
    that needs them; the config home second, for a machine-wide default. Never the
    working directory — a project is named and switched, never inferred from where you
    stand, and an ambient `.env` would reintroduce exactly that.
    """
    paths = []
    if project_dir is not None:
        paths.append(project_dir / FILENAME)
    if home is not None:
        paths.append(home / FILENAME)
    return [path for path in paths if path.is_file()]


def apply(paths: list[Path]) -> list[Path]:
    """Apply the given files, nearest first, without overriding the real environment.

    Shared by the CLI (which discovers the paths) and the MCP server subprocess (which
    is handed them), so both honour precedence identically — the alternative is a child
    that resolves credentials differently from its parent.
    """
    from dotenv import dotenv_values

    present = [path for path in paths if path.is_file()]
    if not present:
        _loaded.clear()
        return []

    # Snapshot first: everything already set is off limits, whichever file mentions it.
    exported = set(os.environ)

    # Lowest precedence last in the list, so iterate reversed and let a nearer file
    # overwrite a farther one here — neither ever overwrites the real environment.
    values: dict[str, str] = {}
    for path in reversed(present):
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})

    for key, value in values.items():
        if key not in exported:
            os.environ[key] = value

    _loaded[:] = present
    return list(present)


def load(project_dir: Path | None = None, home: Path | None = None) -> list[Path]:
    """Find every `.env` that applies and apply it. Returns what was used.

    A credential source that cannot be named is one nobody can debug, which is why this
    reports rather than just acting.
    """
    return apply(candidates(project_dir, home))


def files() -> list[Path]:
    """The `.env` files this process loaded, for handing to a subprocess by path."""
    return list(_loaded)


def reset() -> None:
    """Forget what was loaded. For tests; nothing in the CLI needs it."""
    _loaded.clear()
