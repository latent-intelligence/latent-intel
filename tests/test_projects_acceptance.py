"""The acceptance criterion: project N+1 needs no engine change.

`tests/fixtures/projects/research/` is a complete deployment — its own YAML, logo,
branding, sources and commands. If standing it up required editing `src/`, the whole
project system has failed at its one job, and `test_the_engine_knows_about_no_client`
turns that from a claim into an assertion.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from latent_intel import config as config_module
from latent_intel import project as project_module
from latent_intel import settings as settings_module
from latent_intel.ui import banner
from latent_intel.ui import brand as brand_module
from latent_intel.ui.theme import build_theme

FIXTURE = Path(__file__).parent / "fixtures" / "projects" / "research"


@pytest.fixture
def research(monkeypatch: pytest.MonkeyPatch) -> settings_module.Settings:
    monkeypatch.setenv(project_module.ENV_PROJECTS, str(FIXTURE))
    config = config_module.load()
    config.project = "research"
    config_module.save(config)
    settings_module.invalidate()
    return settings_module.load()


def test_a_new_project_needs_no_engine_change(
    research: settings_module.Settings,
) -> None:
    """Sources, branding and commands all arrive from one directory."""
    assert research.project_name == "research"
    assert [s.id for s in research.sources] == ["notes", "mirror"]
    assert research.project is not None
    assert research.project.commands_dir == FIXTURE / "commands"


def test_a_relative_source_resolves_against_the_project_not_the_cwd(
    research: settings_module.Settings,
) -> None:
    assert research.source("notes").target == str(FIXTURE / "corpus")  # type: ignore[union-attr]


def test_a_project_variable_reaches_a_remote_target(
    research: settings_module.Settings,
) -> None:
    """`vars` is how a backend location is declared without hardcoding a provider."""
    assert (
        research.source("mirror").target
        == "s3://research-mirror-fixture/li/context/raw"
    )  # type: ignore[union-attr]


def test_the_environment_overrides_a_committed_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One project file, a laptop and a deployment host, nothing edited."""
    monkeypatch.setenv("RESEARCH_MIRROR", "s3://deployed/li")
    monkeypatch.setenv(project_module.ENV_PROJECTS, str(FIXTURE))
    config = config_module.load()
    config.project = "research"
    config_module.save(config)
    settings_module.invalidate()

    assert (
        settings_module.load().source("mirror").target == "s3://deployed/li/context/raw"
    )  # type: ignore[union-attr]


def test_branding_reaches_the_banner(research: settings_module.Settings) -> None:
    assert research.project is not None
    brand = brand_module.from_project(
        research.project.branding, research.project.directory
    )
    assert brand.name == "RESEARCH"
    assert brand.prompt_text == "research › "
    assert brand.logo_width > 0 and brand.logo != brand_module.DEFAULT_LOGO

    theme, unknown = build_theme({**brand.theme, **research.theme})
    assert unknown == []
    console = Console(file=io.StringIO(), width=100, force_terminal=False, theme=theme)
    banner.render(console, "0.1.0", brand=brand)
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "standing context, with provenance" in output
    assert max(len(line) for line in output.splitlines()) <= 100


def test_a_user_source_overlays_without_touching_the_project_file(
    research: settings_module.Settings, tmp_path: Path
) -> None:
    """The project file belongs to the deployment. The engine never writes it."""
    before = (FIXTURE / "research.yaml").read_bytes()

    config = config_module.load()
    config.add(
        config_module.SourceSpec(id="scratch", kind="files", target=str(tmp_path)),
        "research",
    )
    config_module.save(config)
    settings_module.invalidate()

    resolved = settings_module.load()
    assert resolved.source("scratch") is not None
    assert resolved.origin["source:scratch"] == "user"
    assert resolved.origin["source:notes"] == "project"
    assert (FIXTURE / "research.yaml").read_bytes() == before


def test_detaching_a_project_source_hides_it_without_editing_the_project(
    research: settings_module.Settings,
) -> None:
    before = (FIXTURE / "research.yaml").read_bytes()

    config = config_module.load()
    config.detach("notes", "research")
    config_module.save(config)
    settings_module.invalidate()

    assert settings_module.load().source("notes") is None
    assert (FIXTURE / "research.yaml").read_bytes() == before


#: Strings only this fixture could have produced. The bare project name is deliberately
#: not one of them: `research` is an ordinary English word that could appear in a
#: docstring tomorrow and fail this test for no reason. These cannot.
FIXTURE_MARKERS = (
    "research-mirror-fixture",
    "RESEARCH_MIRROR",
    "Research Intelligence",
    "standing context, with provenance",
)


def test_the_engine_knows_about_no_client() -> None:
    """The mechanised form of the whole point.

    A deployment exercised by the suite must leave no trace in the shipped package. If
    this fails, something deployment-specific has been hardcoded into the engine.
    """
    source = Path(__file__).parent.parent / "src"
    for marker in FIXTURE_MARKERS:
        hits = subprocess.run(
            ["grep", "-ril", "--exclude-dir=__pycache__", marker, str(source)],
            capture_output=True,
            text=True,
        )
        assert hits.stdout == "", f"'{marker}' found in src/:\n{hits.stdout}"


def test_the_default_registry_is_per_install_not_someones_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A shipped engine must not know where anyone keeps their notes.

    The default used to be one operator's personal path, so a client running the tool
    saw that operator's catalogue in `intel stores`.
    """
    from latent_intel import registry

    monkeypatch.delenv(registry.ENV_VAR, raising=False)
    assert "/space/knowledge" not in registry.DEFAULT_REGISTRY
    assert registry.registry_path().parent == config_module.home()


# -- the default setup ------------------------------------------------------
#
# What a fresh install does, which nothing asserted before. `latent.yaml` is our own
# branding shipped as a project, so the client path is the path we take ourselves and
# cannot rot unnoticed.


def test_a_fresh_install_works_with_no_project_at_all() -> None:
    """No project is a valid state — the engine works, it just has no sources."""
    resolved = settings_module.load()

    assert resolved.project is None
    assert resolved.sources == []
    assert resolved.approval == "ask"
    assert resolved.problems == []


def test_the_shipped_default_is_a_real_project(monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_module.load()
    config.project = "latent"
    config_module.save(config)
    settings_module.invalidate()

    resolved = settings_module.load()
    assert resolved.project_name == "latent"
    assert resolved.problems == []
    # No default store, deliberately: a tool that arrives pointed at something has
    # decided for you.
    assert resolved.sources == []


def test_the_default_banner_renders_at_a_narrow_terminal() -> None:
    """The first thing anyone sees, at the width they are most likely to hit."""
    for width in (100, 60, 40):
        console = Console(
            file=io.StringIO(),
            width=width,
            force_terminal=False,
            theme=build_theme()[0],
        )
        banner.render(console, "0.1.0", brand=brand_module.DEFAULT)
        output = console.file.getvalue()  # type: ignore[attr-defined]
        assert "LATENT" in output or "█" in output
        assert max(len(line) for line in output.splitlines()) <= width
