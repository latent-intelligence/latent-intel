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

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from . import procedures, subagents
from .config import SourceSpec
from .models import Skill

#: How a persona meets our own prompt. `append` adds it after ours; `replace` puts it
#: in place of the posture line — and of that line only, never the sources inventory,
#: which the agent cannot use its tools without. Anything else is a problem and reads
#: as `append`: a misspelled mode should not cost a deployment its voice.
PERSONA_MODES = ("append", "replace")

#: Extra search locations a host sets; `intel deployment add` records them in config.
ENV_PROJECTS = "LATENT_INTEL_PROJECTS"
#: Overrides the active project for one invocation. How two terminals run two projects.
ENV_ACTIVE = "LATENT_INTEL_PROJECT"

#: Names the engine uses for its own states; a project may not take them.
RESERVED = frozenset({"none", "default"})

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Every key a project file may carry at the top level. Anything else is a problem: a
#: misspelt `serv:` that is silently ignored leaves a deployment served the wrong way.
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "title",
        "description",
        "registry",
        "vars",
        "sources",
        "branding",
        "agent",
        "commands",
        "skills",
        "agents",
        "defaults",
        "root",
    }
)


#: The `defaults:` keys this build reads. Another one is a problem: `limit: 8` was
#: silently dropped while every search returned 10.
DEFAULT_KEYS = frozenset({"approval", "web"})


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


def _target(kind: str, value: str, base: Path, variables: dict[str, str]) -> str:
    """A source's `target:`, resolved — unless it is a command line rather than a place.

    An `mcp` target is argv. Joining it to the project directory turned a console script
    into `<project dir>/example-mcp`, which nothing can start, and `npx -y @x/mcp`
    into a single Path with spaces in it. Resolution and a shlex split are incompatible
    operations on one string; a relative executable is what `cwd` is for, and that still
    resolves.
    """
    if kind == "mcp":
        return _substituted(value, variables)
    return _resolve(value, base, variables)


#: Source options that name a place, and so resolve against the project file. `cwd` is
#: where a server starts; `context` (alias `raw`) is the store a wiki's pages cite. A
#: relative one left unresolved would be read against wherever the process was started.
LOCATION_OPTIONS = frozenset({"cwd", "context", "raw"})


def _option(key: str, value: Any, base: Path, variables: dict[str, str]) -> Any:
    """One source option, substituted — and resolved when it names a location.

    `cwd` is a directory a server is started in, so it gets `target`'s rule rather than
    the working directory's: a project cloned anywhere must start its server in the
    directory the file meant, not in the one the operator happened to be standing in.
    """
    if not isinstance(value, str):
        return value
    if key in LOCATION_OPTIONS:
        return _resolve(value, base, variables)
    return _substituted(value, variables)


def _root(
    value: Any, base: Path, variables: dict[str, str], problems: list[str]
) -> Path | None:
    """A declared `root:`, resolved — or `None` and a problem, never the default.

    Falling back to the deployment's directory would put products somewhere the author
    did not ask for. A URI becomes a `UPath`, imported only then, so a local root costs
    no fsspec on start-up; the connectors follow the same rule.
    """
    if not isinstance(value, str) or not value.strip():
        problems.append(f"root: {value!r} is not a path or URI")
        return None
    try:
        resolved = _resolve(value, base, variables)
    except UnsetVariable as exc:
        problems.append(f"root: {exc}")
        return None
    if "://" not in resolved:
        return Path(resolved)
    from upath import UPath

    try:
        return cast(Path, UPath(resolved))
    except ValueError as exc:  # an unknown or mis-cased scheme
        problems.append(f"root: {exc}")
        return None


def _persona(
    agent: dict[str, Any], base: Path, variables: dict[str, str], problems: list[str]
) -> tuple[str, str]:
    """The persona text and the mode it applies in, from a project's `agent:` block.

    A path, resolved against the project file like every other path — a persona that
    only resolves on the machine that authored it is the failure the path rule exists
    for. A file that cannot be read is a problem and an empty persona, never a crash:
    the same non-fatal discipline sources follow.
    """
    mode = str(agent.get("persona_mode") or "append")
    if mode not in PERSONA_MODES:
        problems.append(
            f"persona_mode: '{mode}' is not one of {', '.join(PERSONA_MODES)} — "
            f"reading it as append"
        )
        mode = "append"
    value = agent.get("persona")
    if not value:
        return "", mode
    try:
        text = Path(_resolve(str(value), base, variables)).read_text(encoding="utf-8")
    except (UnsetVariable, OSError, UnicodeDecodeError) as exc:
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`: a persona saved
        # from a word processor in cp1252 would otherwise take every command down.
        problems.append(f"persona: {exc}")
        return "", mode
    return text.strip(), mode


def _skills(directory: Path | None, problems: list[str]) -> list[Skill]:
    """Every skill in a project's skills directory, in name order.

    Two layouts, freely mixed: a flat `name.md`, and the Agent Skills layout
    `name/SKILL.md` that Claude Code, the Agent SDK and MCP-served skills all read — so
    one skills directory serves every one of them. Name order, so what the model is
    given is what the directory listing says. One unreadable skill is reported and
    skipped rather than costing a deployment its other four.

    Both failures this used to be silent about are problems now: a subdirectory with
    no `SKILL.md` loaded nothing and said nothing, and two skills with one name would
    make `load_skill` return whichever came first. The first in name order is kept.
    Only `SKILL.md` is read from a skill directory; its other files are not served.
    """
    found: list[Skill] = []
    if directory is None:
        return found
    seen: set[str] = set()
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            source, fallback = path / "SKILL.md", path.name
            if not source.is_file():
                problems.append(f"skills: {path.name}/ has no SKILL.md")
                continue
        elif path.suffix == ".md":
            source, fallback = path, path.stem
        else:
            continue
        label = source.relative_to(directory).as_posix()
        skill = _skill(source, fallback, label, problems)
        if skill is None:
            continue
        if skill.name in seen:
            problems.append(
                f"skills: {label}: a skill named '{skill.name}' already loaded"
            )
            continue
        seen.add(skill.name)
        found.append(skill)
    return found


def _skill(path: Path, fallback: str, label: str, problems: list[str]) -> Skill | None:
    """One skill file, read with Claude Code's frontmatter and Claude Code's defaults.

    A skill is prose, and requiring a header to have any would be a ceremony with no
    reader. A leading block counts as frontmatter only when it is a mapping with one of
    the skill keys. Frontmatter is optional here, so a prose file that opens with a
    thematic break — `---` above a rule, say — is prose, and consuming its first
    paragraph as metadata would hand the model a skill quietly missing a sentence its
    author wrote.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"skills: {exc}")
        return None
    block, body = procedures.split_frontmatter(text)
    meta: dict[str, Any] = {}
    if block is not None:
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            problems.append(f"skills: {label}: {exc}")
            return None
        if isinstance(parsed, dict) and _SKILL_KEYS & set(parsed):
            meta = parsed
        else:
            body = text  # a thematic break, not a header — see the docstring
    body = body.strip()
    skill = Skill(
        name=str(meta.get("name") or fallback),
        body=body,
        description=_description(meta, body),
        model_invocable=not _flag(meta.get("disable-model-invocation"), False),
        user_invocable=_flag(meta.get("user-invocable"), True),
    )
    problems += [f"skills: {label}: {issue}" for issue in _unhonoured(skill, meta)]
    return skill


def _unhonoured(skill: Skill, meta: dict[str, Any]) -> list[str]:
    """What this skill asks for that this build will not do — said, not dropped.

    A key Claude Code acts on and we ignore (a model override, a forked context, a tool
    grant) would otherwise do nothing while the file says it is on.
    """
    issues: list[str] = []
    if ignored := sorted(_IGNORED_KEYS & set(meta)):
        issues.append(f"ignores {', '.join(ignored)} — this build does not act on them")
    if any(c.isspace() for c in skill.name):
        issues.append(f"'{skill.name}' has a space in it, so /name cannot run it")
    if not skill.model_invocable and not skill.user_invocable:
        issues.append(
            "disable-model-invocation and user-invocable: false together leave nobody "
            "to run it"
        )
    if _POSITIONAL.search(skill.body):
        issues.append(
            "uses $N or $ARGUMENTS[N], which this build does not substitute — "
            "only $ARGUMENTS"
        )
    return issues


#: The frontmatter keys this build acts on.
_HONOURED_KEYS = frozenset(
    {"name", "description", "when_to_use", "disable-model-invocation", "user-invocable"}
)

#: Claude Code's other keys. They still make a block frontmatter; those that change
#: behaviour are reported. `argument-hint`, `metadata`, `license` and `compatibility`
#: change nothing, so ignoring them is not a problem.
_IGNORED_KEYS = frozenset(
    {
        "arguments",
        "allowed-tools",
        "disallowed-tools",
        "model",
        "effort",
        "context",
        "agent",
        "background",
        "hooks",
        "paths",
        "shell",
    }
)

#: The frontmatter keys that make a leading block a skill header rather than prose.
_SKILL_KEYS = (
    _HONOURED_KEYS
    | _IGNORED_KEYS
    | {
        "argument-hint",
        "metadata",
        "license",
        "compatibility",
    }
)

#: Claude Code's positional forms. Only `$ARGUMENTS` itself is substituted here.
_POSITIONAL = re.compile(r"\$ARGUMENTS\[\d+\]|\$\d+\b")

#: Claude Code truncates `description` plus `when_to_use` here in its skill listing.
DESCRIPTION_LIMIT = 1536


def _description(meta: dict[str, Any], body: str) -> str:
    """What the model reads to decide whether to load a skill, as Claude Code builds it.

    `description`, else the body's first non-empty line; `when_to_use` appended; one
    line, whatever the author wrapped it to, since it becomes one line of the listing.
    """
    first = next((line for line in body.splitlines() if line.strip()), "")
    parts = [str(meta.get("description") or first.lstrip("#")), meta.get("when_to_use")]
    text = " ".join(" ".join(str(part).split()) for part in parts if part)
    return text[:DESCRIPTION_LIMIT]


def _flag(value: Any, default: bool) -> bool:
    """A frontmatter boolean, read as Claude Code reads one: `true`/`false`, `yes`/`no`,
    `on`/`off`, `1`/`0`, any case. Anything else is the default."""
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in {"true", "yes", "on", "1"}:
        return True
    if word in {"false", "no", "off", "0"}:
        return False
    return default


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
    #: The deployment's own voice, already read. Text rather than a path, because every
    #: reader of it wants the text and only this module knows where the file was.
    persona: str = ""
    persona_mode: str = "append"
    #: In name order. Listed for the model to load on demand unless marked
    #: `disable-model-invocation`; runnable by hand as `/name` unless `user-invocable:
    #: false`.
    skills: list[Skill] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    #: Delegates, from the `agents:` directory — see `subagents.py`. Read here; run only
    #: by a runtime that declares `agent.base.Delegating`.
    agents: list[subagents.Subagent] = field(default_factory=list)
    #: Where the deployment's products go: `root:`, else the file's directory. A local
    #: path or a remote URI (`s3://…`, a `UPath`). `None` only when a declared `root:`
    #: could not be resolved — a problem says why, and nothing may fall back to writing
    #: elsewhere.
    root: Path | None = None
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
        raise ProjectError(f"{path}: a deployment must be a mapping")

    base = path.parent
    variables = {str(k): str(v) for k, v in (raw.get("vars") or {}).items()}
    problems: list[str] = []
    if unknown := sorted(set(raw) - TOP_LEVEL_KEYS):
        problems.append(
            f"unknown top-level key(s) {', '.join(unknown)} — ignored; expected one of "
            f"{', '.join(sorted(TOP_LEVEL_KEYS))}"
        )

    defaults = dict(raw.get("defaults") or {})
    if unread := sorted(set(defaults) - DEFAULT_KEYS):
        problems.append(
            f"defaults: {', '.join(map(str, unread))} — not acted on; this build reads "
            f"{', '.join(sorted(DEFAULT_KEYS))}"
        )

    name = str(raw.get("name") or path.stem)
    if name in RESERVED:
        problems.append(f"'{name}' is reserved — rename the deployment")

    sources: list[SourceSpec] = []
    for row in raw.get("sources") or []:
        if not isinstance(row, dict) or not row.get("id"):
            problems.append(f"a source with no id was skipped: {row!r}")
            continue
        if not (row.get("from") or row.get("target")):
            problems.append(f"source '{row['id']}' names neither `from` nor `target`")
            continue
        target = row.get("target")
        kind = str(row.get("kind") or "")
        try:
            # Resolved here rather than at attach time: the base directory is this
            # file's, and by attach time nobody remembers where the file was. Options
            # carry locations too (`context:` beside a wiki), so they get the same
            # substitution — a variable honoured in `target:` and ignored one line
            # below it is the quiet kind of wrong.
            resolved = _target(kind, str(target), base, variables) if target else None
            options = {
                str(k): _option(str(k), v, base, variables)
                for k, v in (row.get("options") or {}).items()
            }
        except UnsetVariable as exc:
            problems.append(f"source '{row['id']}' skipped: {exc}")
            continue
        sources.append(
            SourceSpec(
                id=str(row["id"]),
                kind=kind,
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

    agent = dict(raw.get("agent") or {})
    persona, persona_mode = _persona(agent, base, variables, problems)
    skills_dir = directory("skills")
    commands_dir = directory("commands")
    skills = _skills(skills_dir, problems)
    # One `/name` namespace: a skill wins over a project command of the same name, as
    # in Claude Code, and saying so is the difference between a shadow and a mystery.
    commands, _ = procedures.discover(commands_dir)
    problems += [
        f"skills: /{s.name} is also a deployment command; the skill wins"
        for s in skills
        if s.user_invocable and s.name in commands
    ]

    root = _root(raw["root"], base, variables, problems) if "root" in raw else base

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
        agents=subagents.load(directory("agents"), problems),
        variables=variables,
        sources=sources,
        branding=dict(raw.get("branding") or {}),
        agent=agent,
        commands_dir=commands_dir,
        skills_dir=skills_dir,
        persona=persona,
        persona_mode=persona_mode,
        skills=skills,
        defaults=defaults,
        root=root,
        problems=problems,
    )


def check_sources(project: Project) -> list[str]:
    """What would stop each source attaching that can be seen without attaching it.

    A local `files` or `code` target must be a directory, and an `mcp` command must be
    on PATH or, when it names a path, there — under its `cwd`, which is where it starts,
    else the working directory. Nothing is started or read, so a URI, a URL and a
    registry name are left for attach to judge.
    """
    problems: list[str] = []
    for spec in project.sources:
        target = spec.target
        if target is None or "://" in target:
            continue
        if spec.kind in ("files", "code"):
            if not Path(target).is_dir():
                problems.append(f"source '{spec.id}': no directory at {target}")
        elif spec.kind == "mcp":
            try:
                command = shlex.split(target)[0]
            except (ValueError, IndexError):
                problems.append(f"source '{spec.id}': no command in {target!r}")
                continue
            if os.sep in command:
                where = Path(str(spec.options.get("cwd") or "."))
                found = shutil.which(str(where / Path(command).expanduser()))
            else:
                found = shutil.which(command)
            if found is None:
                problems.append(
                    f"source '{spec.id}': {command} is not on PATH"
                    if os.sep not in command
                    else f"source '{spec.id}': no program at {command}"
                )
    return problems


#: A name `create` writes: lowercase, digits and hyphens, so it is the same on every
#: filesystem and needs no quoting where it is typed.
NEW_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

#: The worked example a new deployment starts from, so there is one copy of it.
EXAMPLE = Path(__file__).parent / "data" / "projects" / "latent.yaml"

_DESCRIPTION = re.compile(r"^description: >\n(?:  .*\n)+", re.MULTILINE)

_HEADER = """\
# {name} — a latent-intel deployment, written by `intel deployment new`.
#
# Every field a deployment may set is shown, commented, in the order the loader reads
# it. Relative paths resolve against this file's directory, never the working one.

"""


def create(directory: Path, name: str) -> Path:
    """Write `<directory>/<name>/<name>.yaml` from the worked example, named `name`.

    The folder, the file and `name:` agree, so the deployment is chosen by the name it
    declares. Never writes over anything: an existing folder is refused.
    """
    if not NEW_NAME.fullmatch(name) or name in RESERVED:
        raise ProjectError(
            f"'{name}' is not a deployment name — lowercase letters, digits and "
            "hyphens, not 'none' or 'default'"
        )
    folder = directory.expanduser() / name
    if folder.exists():
        raise ProjectError(f"{folder} already exists")

    example = EXAMPLE.read_text(encoding="utf-8")
    body = example[example.index("schema_version:") :]
    # Quoted: YAML reads a bare `0123`, `true`, `null` or `2026-10-17` as no string.
    title = name.replace("-", " ").title()
    for old, new in (
        ("name: latent\n", f"name: {json.dumps(name)}\n"),
        ("title: Latent\n", f"title: {json.dumps(title)}\n"),
        ("  name: LATENT\n", f"  name: {json.dumps(name.upper())}\n"),
        ('"latent › "', f'"{name} › "'),
    ):
        if body.count(old) != 1:
            raise ProjectError(f"the worked example no longer holds {old.strip()!r}")
        body = body.replace(old, new)
    body, count = _DESCRIPTION.subn(
        "description: >\n  What this deployment is for.\n", body, count=1
    )
    if count != 1:
        raise ProjectError("the worked example no longer holds its description")

    folder.mkdir(parents=True)
    path = folder / f"{name}.yaml"
    path.write_text(_HEADER.format(name=name) + body, encoding="utf-8")
    return path


def search_paths(
    extra: list[str] | None = None, home: Path | None = None
) -> list[Path]:
    """Where projects are looked for, in precedence order.

    A deployment override first, then whatever the user added, then the directory
    `project add --copy` writes to, then what ships with the engine.
    """
    paths = _env_paths()
    paths += [Path(p).expanduser() for p in (extra or [])]
    if home is not None:
        paths.append(home / "projects")
    paths.append(Path(__file__).parent / "data" / "projects")
    return paths


def _env_paths() -> list[Path]:
    value = os.environ.get(ENV_PROJECTS) or ""
    return [Path(p).expanduser() for p in value.split(os.pathsep) if p]


def candidates(directory: Path) -> list[Path]:
    """The deployment files in one search directory: `<name>.yaml` in it, then
    `<name>/<name>.yaml` one folder down — the layout `create` writes, so the folder
    that holds several is added once. A YAML file named otherwise below it is something
    else that lives in a deployment's folder."""
    nested = (p for p in directory.glob("*/*.yaml") if p.stem == p.parent.name)
    return sorted(directory.glob("*.yaml")) + sorted(nested)


def discover(
    extra: list[str] | None = None, home: Path | None = None
) -> tuple[dict[str, Path], list[str]]:
    """Every project findable, by name, plus problems: collisions and missing paths.

    First path wins, and a name found twice is reported rather than silently shadowed:
    two deployments disagreeing about what one name means is the kind of confusion that
    is unbearable to debug later.
    """
    found: dict[str, Path] = {}
    problems: list[str] = []
    configured = {
        **{
            Path(p).expanduser(): "added with `intel deployment add`"
            for p in extra or []
        },
        **{p: f"named in {ENV_PROJECTS}" for p in _env_paths()},
    }
    for directory in search_paths(extra, home):
        if not directory.is_dir():
            # A configured path that does not exist is worth saying out loud; a
            # not-yet-created default directory is not.
            if (why := configured.get(directory)) is not None:
                problems.append(f"{directory} is not a directory — {why}")
            continue
        for candidate in candidates(directory):
            name = candidate.stem
            if name in found:
                problems.append(f"{name}: {candidate} shadowed by {found[name]}")
                continue
            found[name] = candidate
    return found, problems
