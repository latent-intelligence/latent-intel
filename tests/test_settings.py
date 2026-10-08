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
    assert resolved.origin["web"] == "project"


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
