"""A project — one deployment's sources, branding and commands, as data.

The engine is installed once; a project is a YAML file owned by whoever owns the
deployment. That split is the whole point: standing up project N+1 is a file and a
directory, with no code change and no release, and the engine knows about no client.

**Relative paths resolve against the project file's directory, never the working
directory.** A project is meant to be cloned — a client repo checked out anywhere, a
container image, a shared drive — so a path that only resolves where it was authored is
the failure this rule exists to prevent. It is the same discipline `SourceSpec` already
applies to sources: record what was named, resolve at read time.

**`vars` are locations, never secrets**, and an environment variable of the same name
wins. That is how one project file serves a laptop and a deployment host without being
edited, and it is why a bucket can be swapped without touching version control.

This module may not import `registry`, `connectors` or `agent` — enforced by
import-linter. A frontend needs branding and a project name *before* a session exists,
so anything here must be safe for a frontend to read directly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import SourceSpec

#: Where `intel project add` records extra search locations, and what a deployment sets.
ENV_PROJECTS = "LATENT_INTEL_PROJECTS"
#: Overrides the active project for one invocation. How two terminals run two projects.
ENV_ACTIVE = "LATENT_INTEL_PROJECT"

#: Names the engine uses for its own states; a project may not take them.
RESERVED = frozenset({"none", "default"})

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProjectError(Exception):
    """A project file that cannot be read at all. A *bad* project is a `problems` list;
    this is for a file that is not YAML, or not a mapping, or unreadable."""


def substitute(value: str, variables: dict[str, str]) -> str:
    """Replace `${NAME}` from `variables`, but let the environment win.

    Environment first is what makes a project file portable: a deployment exports
    `MIRROR` and the committed default is ignored, with nothing edited.
    """

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name) or variables.get(name) or match.group(0)

    return _VAR.sub(one, value)


class UnsetVariable(LookupError):
    """A `${NAME}` that neither `vars:` nor the environment defines."""


def _substituted(value: str, variables: dict[str, str]) -> str:
    """`substitute`, refusing a value it could not complete.

    Leaving `${LI_S3}` in place and carrying on made a deployment whose variable was
    missing resolve every source to `<project dir>/${LI_S3}/…` — a local path that
    exists nowhere, reported much later as a store with no manifest. A filter that
    silently does nothing is worse than an error, and this is the same rule.
    """
    expanded = substitute(value, variables)
    if missing := _VAR.findall(expanded):
        raise UnsetVariable(
            f"${{{missing[0]}}} is not defined — add it under `vars:` or export it"
        )
    return expanded


def _resolve(value: str, base: Path, variables: dict[str, str]) -> str:
    """A path or URI from a project file, made usable.

    A URI is left alone — `s3://bucket/k` is already absolute and joining it to a
    directory would corrupt it, which is the bug this codebase has now shipped four
    times in other guises.
    """
    expanded = _substituted(value, variables)
    if "://" in expanded:
        return expanded
    path = Path(expanded).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


@dataclass
class Project:
    """One deployment, as loaded. Never written by the engine."""

    name: str
    path: Path
    title: str = ""
    description: str = ""
    #: A registry this project's names resolve against, overriding KNOWLEDGE_REGISTRY.
    registry: str | None = None
    variables: dict[str, str] = field(default_factory=dict)
    sources: list[SourceSpec] = field(default_factory=list)
    branding: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)
    commands_dir: Path | None = None
    skills_dir: Path | None = None
    defaults: dict[str, Any] = field(default_factory=dict)
    #: Validation complaints. Non-fatal by design: a project with one bad source should
    #: still attach the other four, and `doctor` is where you go to find out why.
    problems: list[str] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        return self.path.parent


def load(path: Path | str) -> Project:
    """Read one project file. Raises only when the file itself is unusable."""
    path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ProjectError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectError(f"{path}: a project must be a mapping")

    base = path.parent
    variables = {str(k): str(v) for k, v in (raw.get("vars") or {}).items()}
    problems: list[str] = []

    name = str(raw.get("name") or path.stem)
    if name in RESERVED:
        problems.append(f"'{name}' is reserved — rename the project")

    sources: list[SourceSpec] = []
    for row in raw.get("sources") or []:
        if not isinstance(row, dict) or not row.get("id"):
            problems.append(f"a source with no id was skipped: {row!r}")
            continue
        if not (row.get("from") or row.get("target")):
            problems.append(f"source '{row['id']}' names neither `from` nor `target`")
            continue
        target = row.get("target")
        try:
            # Resolved here rather than at attach time: the base directory is this
            # file's, and by attach time nobody remembers where the file was. Options
            # carry locations too (`context:` beside a wiki), so they get the same
            # substitution — a variable honoured in `target:` and ignored one line
            # below it is the quiet kind of wrong.
            resolved = _resolve(str(target), base, variables) if target else None
            options = {
                str(k): _substituted(v, variables) if isinstance(v, str) else v
                for k, v in (row.get("options") or {}).items()
            }
        except UnsetVariable as exc:
            problems.append(f"source '{row['id']}' skipped: {exc}")
            continue
        sources.append(
            SourceSpec(
                id=str(row["id"]),
                kind=str(row.get("kind") or ""),
                from_registry=str(row["from"]) if row.get("from") else None,
                target=resolved,
                remote=bool(row.get("remote")),
                options=options,
            )
        )

    def directory(key: str) -> Path | None:
        value = raw.get(key)
        if not value:
            return None
        try:
            resolved = Path(_resolve(str(value), base, variables))
        except UnsetVariable as exc:
            problems.append(f"{key}: {exc}")
            return None
        if not resolved.is_dir():
            problems.append(f"{key}: {resolved} is not a directory")
            return None
        return resolved

    registry: str | None = None
    if registry_value := raw.get("registry"):
        try:
            registry = _resolve(str(registry_value), base, variables)
        except UnsetVariable as exc:
            problems.append(f"registry: {exc}")

    return Project(
        name=name,
        path=path,
        title=str(raw.get("title") or name),
        description=str(raw.get("description") or "").strip(),
        registry=registry,
        variables=variables,
        sources=sources,
        branding=dict(raw.get("branding") or {}),
        agent=dict(raw.get("agent") or {}),
        commands_dir=directory("commands"),
        skills_dir=directory("skills"),
        defaults=dict(raw.get("defaults") or {}),
        problems=problems,
    )


def search_paths(
    extra: list[str] | None = None, home: Path | None = None
) -> list[Path]:
    """Where projects are looked for, in precedence order.

    A deployment override first, then whatever the user added, then the directory
    `project add --copy` writes to, then what ships with the engine.
    """
    paths: list[Path] = []
    if env := os.environ.get(ENV_PROJECTS):
        paths += [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    paths += [Path(p).expanduser() for p in (extra or [])]
    if home is not None:
        paths.append(home / "projects")
    paths.append(Path(__file__).parent / "data" / "projects")
    return paths


def discover(
    extra: list[str] | None = None, home: Path | None = None
) -> tuple[dict[str, Path], list[str]]:
    """Every project findable, by name, plus any collisions.

    First path wins, and a name found twice is reported rather than silently shadowed:
    two deployments disagreeing about what one name means is the kind of confusion that
    is unbearable to debug later.
    """
    found: dict[str, Path] = {}
    collisions: list[str] = []
    for directory in search_paths(extra, home):
        if not directory.is_dir():
            # A configured path that does not exist is worth saying out loud; a
            # not-yet-created default directory is not.
            continue
        for candidate in sorted(directory.glob("*.yaml")):
            name = candidate.stem
            if name in found:
                collisions.append(f"{name}: {candidate} shadowed by {found[name]}")
                continue
            found[name] = candidate
    return found, collisions
