"""What this user has attached, and where their state lives.

A CLI is a new process every time, so "attached" cannot live in memory the way it does
in the shell. `intel connect design` records the source here; `intel search` reads it
back and attaches before running. Without that, every command would need its sources
named on the command line.

Layout follows `reference-architectures/cli.md`, including two rules that matter more
than they look:

    ~/.config/latent-intel/config.yaml     configuration the user edits
    ~/.local/share/latent-intel/           data the tool manages
    LATENT_INTEL_HOME                      overrides both — needed for tests and for
                                           multi-tenant hosts

**Credentials never live in this file.** Runtimes read keys from the environment. A
config file gets copied into a gist, committed to a dotfiles repo and pasted into a
support thread; an environment variable does not.

**Nothing is written inside the install directory.** It gets replaced on upgrade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "config.yaml"
ENV_HOME = "LATENT_INTEL_HOME"


@dataclass
class SourceSpec:
    """One attached source — recorded as it was *named*, not as it resolved.

    This distinction is the whole point of the shape. An earlier version stored the
    resolved path, so a config read `/Users/x/Library/CloudStorage/GoogleDrive-x@y.com/
    My Drive/knowledge/wiki` — tied to one machine, and carrying an email address into a
    file people paste into chats. Now a registered store records its *registry id* and
    re-resolves wherever it is read, and a literal path is kept exactly as typed, with
    `~` and `${VAR}` intact.
    """

    id: str
    kind: str
    #: A registry id. When set, `target` is ignored and resolution happens at attach
    #: time — so the same config works on a laptop with Drive and a host without it.
    from_registry: str | None = None
    #: A path or URI, as typed. Only used when `from_registry` is unset.
    target: str | None = None
    #: Prefer the registry's mirror over any local copy.
    remote: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> str:
        """What to hand `Session.connect`."""
        return self.from_registry or self.target or ""

    @property
    def by_name(self) -> bool:
        """True when the registry resolves it, so no kind needs passing."""
        return self.from_registry is not None


@dataclass
class Overlay:
    """What a *user* changed about one project, without touching the project file.

    `intel connect` writes here. `intel disconnect` removes an overlay source, or — for
    a source the project declares — records it in `detached`, because the engine must
    never write to a file the deployment owns.
    """

    sources: list[SourceSpec] = field(default_factory=list)
    detached: list[str] = field(default_factory=list)


@dataclass
class Config:
    #: Which project is active. `None` means none, which is a valid state: the engine
    #: works with no project, it just has no sources.
    project: str | None = None
    #: Extra directories (or files) to search for projects, added by `project add`.
    project_paths: list[str] = field(default_factory=list)
    #: Per-project user changes, keyed by project name.
    overlays: dict[str, Overlay] = field(default_factory=dict)
    #: Theme token overrides, merged over the project's and the engine's.
    theme: dict[str, str] = field(default_factory=dict)
    #: A pre-projects `sources:` block, kept verbatim until `project migrate` runs.
    #:
    #: Neither obvious alternative is safe. Dropping it from the known set sweeps it
    #: into `extra`, which `save` round-trips while *nothing attaches* — the file still
    #: looks right. Keeping it known while no longer populating it is worse: `save`
    #: rebuilds the payload from scratch, so the block is deleted on the next keystroke
    #: that writes config. Silent no-op, or silent loss. Hence: verbatim, until migrate.
    legacy_sources: list[SourceSpec] = field(default_factory=list)
    #: Which agent runtime to use. None means none configured, which `ask` reports.
    runtime: str | None = None
    #: Per-runtime settings, keyed by runtime name. Nested rather than flat because a
    #: model name means nothing without the runtime it belongs to: `model: opus` is
    #: wrong the moment you switch to openai, and a flat key would go stale in
    #: silence. Nesting also means switching back remembers what you had.
    runtimes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: `ask` before anything that writes, `never` to refuse writes outright, `auto` to
    #: allow declared-safe effects through. The default is the cautious one.
    approval: str = "ask"
    #: Top-level keys this build does not recognise, kept so that writing the file never
    #: deletes something a person put there by hand. `save` rebuilds the payload from
    #: scratch, which was harmless while only `connect` wrote it and is not once a
    #: keystroke does.
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def runtime_options(self, kind: str | None = None) -> dict[str, Any]:
        """Settings for one runtime, defaulting to the configured one."""
        name = kind or self.runtime
        return dict(self.runtimes.get(name or "", {}))

    def model_for(self, kind: str | None = None) -> str | None:
        """The model chosen for one runtime, if any."""
        value = self.runtime_options(kind).get("model")
        return str(value) if value else None

    def set_runtime_option(self, kind: str, key: str, value: str | None) -> None:
        """Set or clear one runtime's option, leaving no empty section behind."""
        section = self.runtimes.setdefault(kind, {})
        if value is None:
            section.pop(key, None)
        else:
            section[key] = value
        if not section:
            self.runtimes.pop(kind, None)

    def overlay(self, project: str | None = None) -> Overlay:
        """The user's changes to one project, created on first use.

        `None` is a real project — the no-project state still lets someone attach a
        source — and it is keyed as `default` so it round-trips through YAML.
        """
        return self.overlays.setdefault(project or "default", Overlay())

    def find(self, source_id: str, project: str | None = None) -> SourceSpec | None:
        return next(
            (s for s in self.overlay(project).sources if s.id == source_id), None
        )

    def add(self, spec: SourceSpec, project: str | None = None) -> None:
        """Attach in the user layer, replacing any overlay source under that id."""
        overlay = self.overlay(project)
        overlay.sources = [s for s in overlay.sources if s.id != spec.id] + [spec]
        overlay.detached = [d for d in overlay.detached if d != spec.id]

    def remove(self, source_id: str, project: str | None = None) -> bool:
        """Drop a user-added source. Returns False if there was none — a *project*
        source is detached instead, which the caller decides because only it knows
        whether the project declares one."""
        overlay = self.overlay(project)
        before = len(overlay.sources)
        overlay.sources = [s for s in overlay.sources if s.id != source_id]
        return len(overlay.sources) < before

    def detach(self, source_id: str, project: str | None = None) -> None:
        """Hide a source the project declares, without writing to the project file."""
        overlay = self.overlay(project)
        if source_id not in overlay.detached:
            overlay.detached.append(source_id)


def home() -> Path:
    """The configuration directory. `LATENT_INTEL_HOME` overrides it wholesale."""
    if override := os.environ.get(ENV_HOME):
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "latent-intel"


def data_home() -> Path:
    """Where history and caches go — never beside the configuration."""
    if override := os.environ.get(ENV_HOME):
        return Path(override).expanduser() / "data"
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "latent-intel"


def config_path() -> Path:
    return home() / CONFIG_NAME


def load() -> Config:
    """Read the config. A missing or unreadable file is an empty config, not an error —
    a first run should work, and a corrupt file should not make the program unusable."""
    path = config_path()
    if not path.is_file():
        return Config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return Config()
    if not isinstance(data, dict):
        return Config()

    def rows(raw: Any) -> list[SourceSpec]:
        return [
            SourceSpec(
                id=str(row.get("id") or ""),
                kind=str(row.get("kind") or ""),
                # `from` reads better in YAML than `from_registry`; it is a Python
                # keyword, which is the only reason the field is not called that.
                from_registry=str(row["from"]) if row.get("from") else None,
                target=str(row["target"]) if row.get("target") else None,
                remote=bool(row.get("remote")),
                options=dict(row.get("options") or {}),
            )
            for row in (raw or [])
            if isinstance(row, dict)
            and row.get("id")
            and (row.get("from") or row.get("target"))
        ]

    overlays: dict[str, Overlay] = {}
    for name, body in (data.get("overlays") or {}).items():
        if not isinstance(body, dict):
            continue
        overlays[str(name)] = Overlay(
            sources=rows(body.get("sources")),
            detached=[str(d) for d in (body.get("detached") or [])],
        )

    known = {
        "sources",
        "runtime",
        "runtimes",
        "approval",
        "project",
        "project_paths",
        "overlays",
        "theme",
    }
    runtimes = {
        str(name): dict(options)
        for name, options in (data.get("runtimes") or {}).items()
        if isinstance(options, dict)
    }
    return Config(
        project=data.get("project") or None,
        project_paths=[str(p) for p in (data.get("project_paths") or [])],
        overlays=overlays,
        theme={str(k): str(v) for k, v in (data.get("theme") or {}).items()},
        legacy_sources=rows(data.get("sources")),
        runtime=data.get("runtime") or None,
        runtimes=runtimes,
        approval=str(data.get("approval") or "ask"),
        extra={k: v for k, v in data.items() if k not in known},
    )


def _row(source: SourceSpec) -> dict[str, Any]:
    """One source as YAML. Only what was given — no resolved paths, no empty keys."""
    row: dict[str, Any] = {"id": source.id, "kind": source.kind}
    if source.from_registry:
        row["from"] = source.from_registry
    if source.target:
        row["target"] = source.target
    if source.remote:
        row["remote"] = True
    if source.options:
        row["options"] = source.options
    return row


def save(config: Config) -> Path:
    """Write the user layer. Invalidates the resolved view, which is cached per process
    and would otherwise answer from before the write."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unrecognised keys first, so anything this build knows about still wins.
    payload: dict[str, Any] = dict(config.extra)
    # Emitted verbatim until `project migrate` runs. Writing it back is what stops a
    # keystroke deleting a block this build no longer populates.
    if config.legacy_sources:
        payload["sources"] = [_row(source) for source in config.legacy_sources]
    if config.project:
        payload["project"] = config.project
    if config.project_paths:
        payload["project_paths"] = config.project_paths
    payload["approval"] = config.approval
    if config.runtime:
        payload["runtime"] = config.runtime
    if config.runtimes:
        payload["runtimes"] = config.runtimes
    if config.theme:
        payload["theme"] = config.theme
    live = {
        name: overlay
        for name, overlay in config.overlays.items()
        if overlay.sources or overlay.detached
    }
    if live:
        payload["overlays"] = {
            name: {
                **({"sources": [_row(s) for s in o.sources]} if o.sources else {}),
                **({"detached": o.detached} if o.detached else {}),
            }
            for name, o in live.items()
        }
    from . import settings as settings_module

    settings_module.invalidate()
    path.write_text(
        "# latent-intel — attached sources and preferences.\n"
        "# Credentials belong in the environment, never here.\n\n"
        + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path
