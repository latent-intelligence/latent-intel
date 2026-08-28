"""Config: the nested per-runtime schema, and not losing what it does not understand."""

from __future__ import annotations

from pathlib import Path

import pytest

from latent_intel import config


def test_a_model_is_remembered_per_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switch away and back, and the first model is still there.

    This is why the schema nests `model` under a runtime rather than sitting flat beside
    it: a model name means nothing to a different backend, so a flat key would go stale
    in silence the moment you switched.
    """
    monkeypatch.setenv("LATENT_INTEL_HOME", str(tmp_path))
    settings = config.Config()
    settings.runtime = "claude-cli"
    settings.set_runtime_option("claude-cli", "model", "sonnet")
    settings.set_runtime_option("openrouter", "model", "anthropic/claude-3.5")
    config.save(settings)

    reloaded = config.load()
    assert reloaded.model_for("claude-cli") == "sonnet"
    assert reloaded.model_for("openrouter") == "anthropic/claude-3.5"
    assert reloaded.model_for() == "sonnet"  # defaults to the configured runtime


def test_clearing_a_model_leaves_no_empty_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATENT_INTEL_HOME", str(tmp_path))
    settings = config.Config()
    settings.set_runtime_option("claude-cli", "model", "sonnet")
    settings.set_runtime_option("claude-cli", "model", None)
    assert settings.runtimes == {}


def test_saving_preserves_a_key_this_build_does_not_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`save` rebuilds the payload from scratch. That was harmless while only `connect`
    wrote the file, and is data loss once a keystroke does."""
    monkeypatch.setenv("LATENT_INTEL_HOME", str(tmp_path))
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("approval: ask\nsomething_new: keep me\n", encoding="utf-8")

    config.save(config.load())
    assert "keep me" in path.read_text(encoding="utf-8")
