"""The web scope across the three layers: engine, project, user — and a bad value at
any of them resolving to `off`, reported, rather than to the layer beneath."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from latent_intel import config as config_module
from latent_intel import settings as settings_module
from latent_intel.models import WebScope


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    directory = tmp_path / "projects"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "deploy.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "deploy")
    settings_module.invalidate()


def _user(web: object) -> None:
    config = config_module.load()
    config.web = web
    config_module.save(config)


def test_the_web_is_off_when_nothing_sets_it() -> None:
    resolved = settings_module.load()
    assert resolved.web == WebScope()
    assert resolved.origin["web"] == "engine"


def test_a_project_default_is_read_in_either_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, monkeypatch, "defaults: {web: search}\n")
    assert settings_module.load().web.mode == "search"

    _project(
        tmp_path,
        monkeypatch,
        "defaults:\n  web: {mode: browse, allowed_domains: [arxiv.org], max_uses: 5}\n",
    )
    resolved = settings_module.load()
    assert resolved.web == WebScope(
        mode="browse", allowed_domains=["arxiv.org"], max_uses=5
    )
    assert resolved.origin["web"] == "deployment"


def test_the_user_layer_wins_over_the_project_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, monkeypatch, "defaults: {web: browse}\n")
    _user(False)  # `web: off`, as YAML hands it over
    resolved = settings_module.load()
    assert resolved.web.mode == "off"
    assert resolved.origin["web"] == "user"


def test_a_bad_scope_is_a_problem_and_the_web_is_off_not_the_layer_beneath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user narrowing a project's open `browse` with a mistyped domain must not get
    the open one."""
    _project(tmp_path, monkeypatch, "defaults: {web: browse}\n")
    _user({"mode": "browse", "allowed_domains": ["https://arxiv.org"]})
    resolved = settings_module.load()
    assert resolved.web.mode == "off"
    assert any("plain hostname" in p and "(user)" in p for p in resolved.problems)


def test_the_user_s_scope_survives_a_save_by_something_else() -> None:
    _user({"mode": "search", "blocked_domains": ["b.example.org"]})
    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)
    written = yaml.safe_load(config_module.config_path().read_text())
    assert written["web"] == {"mode": "search", "blocked_domains": ["b.example.org"]}


def test_a_scope_with_a_key_that_is_not_a_string_is_a_problem_not_a_crash() -> None:
    _user({"mode": "search", 1: "x", "domains": ["a.org"]})
    resolved = settings_module.load()
    assert resolved.web.mode == "off"
    assert any("unknown key" in p for p in resolved.problems)


def test_a_deployment_loaded_by_path_is_not_made_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, monkeypatch, "defaults: {web: search}\n")
    other = tmp_path / "other.yaml"
    other.write_text("name: other\ndefaults: {web: browse}\n", encoding="utf-8")

    assert settings_module.load(path=other).project_name == "other"
    assert settings_module.load(path=other).web.mode == "browse"
    assert settings_module.load().project_name == "deploy"


def test_a_deployment_by_path_keeps_its_user_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay is kept under a deployment's name, so loading it by path finds the
    sources its user added, as `intel deployment use` would have."""
    other = tmp_path / "other.yaml"
    other.write_text("name: other\n", encoding="utf-8")
    config = config_module.load()
    config.overlays["other"] = config_module.Overlay(
        sources=[config_module.SourceSpec(id="notes", kind="files", target="~/notes")]
    )
    config_module.save(config)

    assert [s.id for s in settings_module.load(path=other).sources] == ["notes"]


def test_a_broken_deployment_by_path_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    resolved = settings_module.load(path=broken)
    assert resolved.project is None
    assert any("broken.yaml" in p for p in resolved.problems)


# -- the deployment the environment names -----------------------------------


@pytest.mark.parametrize("command", [["search", "x"], ["stores"], ["doctor"]])
def test_a_misspelt_deployment_in_the_environment_is_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """It attaches nothing, so every command that attaches says why — not only
    `deployment show`. The variable is named, since nothing on screen chose it."""
    from typer.testing import CliRunner

    from latent_intel.frontends.cli.main import app

    _project(tmp_path, monkeypatch, "name: deploy\n")
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "deplyo")
    settings_module.invalidate()

    result = CliRunner().invoke(app, command)
    err = " ".join(result.stderr.split())
    assert "'deplyo' not found" in err
    assert "LATENT_INTEL_PROJECT" in err


def test_none_in_the_environment_keeps_the_user_s_own_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LATENT_INTEL_PROJECT=none` is `deployment use none` for one command: no
    deployment, and the sources attached with none active — not a deployment called
    `none` that nobody can find."""
    config = config_module.load()
    config.deployment = "deploy"
    config.add(config_module.SourceSpec(id="mine", kind="files", target="~/notes"))
    config_module.save(config)
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "none")
    settings_module.invalidate()

    resolved = settings_module.load()
    assert resolved.chosen is None and resolved.project is None
    assert [s.id for s in resolved.sources] == ["mine"]
    assert resolved.problems == []
