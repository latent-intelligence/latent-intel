"""The three layers, resolved into one view.

    engine default  →  deployment  →  user

Per key, the later layer wins. Two keys merge instead: **sources** (the deployment's
list, plus the user's additions, minus what the user detached) and **theme** (token by
token).

`origin` records which layer each value came from. "Why is this source here?" is the
question layering invites, and answering it with a guess is worse than not layering at
all. `intel deployment show` and `doctor` read it.

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
from . import subagents as subagents_module
from .config import SourceSpec
from .models import Skill, WebScope

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
    #: How much of the open web a turn may reach. Layered like `approval`; a session
    #: may narrow or widen it for itself without writing it anywhere.
    web: WebScope = field(default_factory=lambda: WebScope())
    #: The deployment's voice and standing knowledge, as the project declared them. No
    #: user layer: a project is composition, and the engine writes only the user's own
    #: config — so there is nothing here for a later layer to win over.
    persona: str = ""
    persona_mode: str = "append"
    skills: list[Skill] = field(default_factory=list)
    #: The project's delegates, for a runtime that runs them. Project-only, like the
    #: persona: composition has no user layer.
    agents: list[subagents_module.Subagent] = field(default_factory=list)
    theme: dict[str, str] = field(default_factory=dict)
    registry_path: str | None = None
    #: The name the user's overlay for this deployment is kept under — the name it is
    #: found and chosen by, which is its file's — so a writer keys it as this reads it.
    overlay_key: str = "default"
    #: key -> "engine" | "deployment" | "user", for `deployment show`.
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

    return os.environ.get(project_module.ENV_ACTIVE) or config.deployment


def load(name: str | None = None, *, path: Path | None = None) -> Settings:
    """Resolve the layers. Cached per process; `invalidate()` after any write.

    `path` loads one deployment file directly, as a batch eval run does for each
    variant, without making it the active one. Its user overlay is the one kept under
    its name, as it would be had it been chosen with `intel deployment use`.
    """
    config = config_module.load()
    chosen = name or _active(config)
    key = f"path:{path.expanduser().resolve()}" if path else f"settings:{chosen}"
    if name is None and (cached := _CACHE.get(key)) is not None:
        assert isinstance(cached, Settings)
        return cached

    problems: list[str] = []
    origin: dict[str, str] = {}
    loaded: project_module.Project | None = None

    if path is not None:
        try:
            loaded = project_module.load(path)
            problems += loaded.problems
        except project_module.ProjectError as exc:
            problems.append(str(exc))
        # The name `intel deployment use` would choose it by, which is its file's:
        # the overlay is kept under that, whatever its `name:` says.
        chosen = path.stem
    elif chosen:
        found, collisions = project_module.discover(
            config.deployment_paths, config_module.home()
        )
        problems += collisions
        path = found.get(chosen)
        if path is None:
            problems.append(
                f"deployment '{chosen}' not found — `intel deployment list`"
            )
        else:
            try:
                loaded = project_module.load(path)
                problems += loaded.problems
            except project_module.ProjectError as exc:
                # Engine defaults only, and say why. Never fatal.
                problems.append(str(exc))

    settings = Settings(project=loaded, problems=problems, origin=origin)

    # -- sources: project, plus user additions, minus user detachments ---------
    settings.overlay_key = chosen or "default"
    overlay = config.overlays.get(settings.overlay_key, config_module.Overlay())
    declared = loaded.sources if loaded else []
    sources = [s for s in declared if s.id not in overlay.detached]
    for key_source in overlay.sources:
        sources = [s for s in sources if s.id != key_source.id] + [key_source]
        origin[f"source:{key_source.id}"] = "user"
    for source in declared:
        origin.setdefault(f"source:{source.id}", "deployment")

    # Named, never attached and never dropped: `save` keeps the block, so moving it
    # into a deployment by hand loses nothing.
    if (old := config.extra.get("sources")) is not None:
        rows = old if isinstance(old, list) else []
        ids = [str(row.get("id")) for row in rows if isinstance(row, dict)]
        problems.append(
            "sources: at the top of config.yaml is no longer read"
            + (f" ({', '.join(ids)})" if ids else "")
            + " — move them into a deployment's sources:"
        )
    settings.sources = sources

    # -- scalars: user beats project beats engine -----------------------------
    defaults = dict(loaded.defaults) if loaded else {}
    agent = dict(loaded.agent) if loaded else {}

    settings.approval = str(config.approval or defaults.get("approval") or "ask")
    origin["approval"] = (
        "user"
        if config.approval != "ask"
        else ("deployment" if defaults.get("approval") else "engine")
    )

    # A value that does not parse resolves to `off`, not to the layer beneath: a user
    # narrowing a project's open `browse` with a mistyped domain must not get the open
    # one. Reported, never fatal — `doctor` has to work on the machine that has it.
    layer, raw = (
        ("user", config.web)
        if config.web is not None
        else ("deployment", defaults["web"])
        if defaults.get("web") is not None
        else ("engine", None)
    )
    try:
        settings.web = WebScope.parse(raw)
    except ValueError as exc:
        problems.append(f"{exc} ({layer}) — the web is off until it is fixed")
    origin["web"] = layer

    settings.runtime = config.runtime or agent.get("runtime")
    origin["runtime"] = (
        "user"
        if config.runtime
        else ("deployment" if agent.get("runtime") else "engine")
    )

    project_runtimes = {
        str(k): dict(v) for k, v in (agent.get("runtimes") or {}).items()
    }
    for kind, options in config.runtimes.items():
        project_runtimes.setdefault(kind, {}).update(options)
    # What this install holds for this deployment alone — the agent `intel managed
    # sync` created — last, since it is the most specific layer there is.
    # Only what sync records: the overlay is not a second place to configure a runtime.
    for kind, options in overlay.runtimes.items():
        if "synced" in options:
            project_runtimes.setdefault(kind, {})["synced"] = options["synced"]
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

    # -- the deployment's voice ------------------------------------------------
    settings.persona = loaded.persona if loaded else ""
    settings.persona_mode = loaded.persona_mode if loaded else "append"
    settings.skills = list(loaded.skills) if loaded else []
    settings.agents = list(loaded.agents) if loaded else []

    settings.registry_path = loaded.registry if loaded else None

    if name is None:
        _CACHE[key] = settings
    return settings
