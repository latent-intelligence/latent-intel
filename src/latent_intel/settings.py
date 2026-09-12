"""The three layers, resolved into one view.

    engine default  →  project  →  user

Per key, the later layer wins. Two keys merge instead: **sources** (the project's list,
plus the user's additions, minus what the user detached) and **theme** (token by token).

`origin` records which layer each value came from. "Why is this source here?" is the
question layering invites, and answering it with a guess is worse than not layering at
all. `intel project show` and `doctor` read it.

**A broken project must not brick the tool.** One that fails to parse yields
engine-defaults-only with the error in `problems`. `intel doctor` has to work on the
machine whose project file is malformed — that is the machine where you need it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as config_module
from . import project as project_module
from .config import SourceSpec

#: One resolved view per process. `config.load()` is called up to four times in a single
#: `/runtime` keystroke and three times in `doctor`; this is the fix for all of them.
_CACHE: dict[str, Any] = {}


def invalidate() -> None:
    """Forget the resolved view. Called by `config.save`, and by tests."""
    _CACHE.clear()


@dataclass
class Settings:
    """What the program actually runs on."""

    project: project_module.Project | None = None
    sources: list[SourceSpec] = field(default_factory=list)
    runtime: str | None = None
    runtimes: dict[str, dict[str, Any]] = field(default_factory=dict)
    approval: str = "ask"
    theme: dict[str, str] = field(default_factory=dict)
    registry_path: str | None = None
    #: key -> "engine" | "project" | "user", for `project show`.
    origin: dict[str, str] = field(default_factory=dict)
    #: Non-fatal complaints: a bad project file, a shadowed name, a legacy block.
    problems: list[str] = field(default_factory=list)

    @property
    def project_name(self) -> str | None:
        return self.project.name if self.project else None

    def source(self, source_id: str) -> SourceSpec | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def runtime_options(self, kind: str | None = None) -> dict[str, Any]:
        return dict(self.runtimes.get(kind or self.runtime or "", {}))

    def model_for(self, kind: str | None = None) -> str | None:
        value = self.runtime_options(kind).get("model")
        return str(value) if value else None


def _active(config: config_module.Config) -> str | None:
    """Which project, honouring the per-invocation override.

    The environment variable is not an afterthought: it is how two terminals run two
    projects at once, and how CI drives one without touching a home directory.
    """
    import os

    return os.environ.get(project_module.ENV_ACTIVE) or config.project


def load(name: str | None = None) -> Settings:
    """Resolve the layers. Cached per process; `invalidate()` after any write."""
    config = config_module.load()
    chosen = name or _active(config)
    key = f"settings:{chosen}"
    if name is None and (cached := _CACHE.get(key)) is not None:
        assert isinstance(cached, Settings)
        return cached

    problems: list[str] = []
    origin: dict[str, str] = {}
    loaded: project_module.Project | None = None

    if chosen:
        found, collisions = project_module.discover(
            config.project_paths, config_module.home()
        )
        problems += collisions
        path = found.get(chosen)
        if path is None:
            problems.append(f"project '{chosen}' not found — `intel project list`")
        else:
            try:
                loaded = project_module.load(path)
                problems += loaded.problems
            except project_module.ProjectError as exc:
                # Engine defaults only, and say why. Never fatal.
                problems.append(str(exc))

    settings = Settings(project=loaded, problems=problems, origin=origin)

    # -- sources: project, plus user additions, minus user detachments ---------
    overlay = config.overlays.get(chosen or "default", config_module.Overlay())
    declared = loaded.sources if loaded else []
    sources = [s for s in declared if s.id not in overlay.detached]
    for key_source in overlay.sources:
        sources = [s for s in sources if s.id != key_source.id] + [key_source]
        origin[f"source:{key_source.id}"] = "user"
    for source in declared:
        origin.setdefault(f"source:{source.id}", "project")

    # A pre-projects `sources:` block keeps working until `project migrate` runs, so an
    # unmigrated install behaves exactly as it did.
    if config.legacy_sources:
        known = {s.id for s in sources}
        for source in config.legacy_sources:
            if source.id not in known:
                sources.append(source)
                origin[f"source:{source.id}"] = "legacy"
        problems.append(
            "sources: at the top of config.yaml is legacy — run `intel project migrate`"
        )
    settings.sources = sources

    # -- scalars: user beats project beats engine -----------------------------
    defaults = dict(loaded.defaults) if loaded else {}
    agent = dict(loaded.agent) if loaded else {}

    settings.approval = str(config.approval or defaults.get("approval") or "ask")
    origin["approval"] = (
        "user"
        if config.approval != "ask"
        else ("project" if defaults.get("approval") else "engine")
    )

    settings.runtime = config.runtime or agent.get("runtime")
    origin["runtime"] = (
        "user" if config.runtime else ("project" if agent.get("runtime") else "engine")
    )

    project_runtimes = {
        str(k): dict(v) for k, v in (agent.get("runtimes") or {}).items()
    }
    for kind, options in config.runtimes.items():
        project_runtimes.setdefault(kind, {}).update(options)
    settings.runtimes = project_runtimes
    # Approval is resolved across all three layers and belongs to the session, not to
    # one runtime. Named rather than silently dropped: a file that says writes are
    # allowed while every write is withheld is the failure this whole layer exists for.
    for kind, options in project_runtimes.items():
        if "approval" in options:
            problems.append(
                f"approval under runtimes:{kind} is ignored — set it at the top level"
            )

    # -- theme: token by token ------------------------------------------------
    branding = dict(loaded.branding) if loaded else {}
    theme = {str(k): str(v) for k, v in (branding.get("theme") or {}).items()}
    theme.update(config.theme)
    settings.theme = theme

    settings.registry_path = loaded.registry if loaded else None

    if name is None:
        _CACHE[key] = settings
    return settings


def project_home() -> Path:
    """Where `intel project migrate` and `project add --copy` write."""
    return config_module.home() / "projects"
