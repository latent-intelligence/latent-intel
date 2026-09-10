"""Isolation every test gets, whether it asks for one or not.

Before this file, only `LATENT_INTEL_HOME` was isolated, and only for `test_cli.py`.
`KNOWLEDGE_REGISTRY` was isolated nowhere — so a test on a developer machine could see
seven real store ids through `Session.available()`, pass here, and fail anywhere else.
A test that reads the machine it runs on is not a test.

Autouse and session-wide: an opt-in fixture is one someone forgets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from latent_intel import config as config_module
from latent_intel import registry as registry_module


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test reads or writes real state — config, data, projects or registry.

    Each variable is pointed at a path under `tmp_path` rather than merely unset: an
    unset `KNOWLEDGE_REGISTRY` falls back to `DEFAULT_REGISTRY`, which is a real file on
    a developer machine. Absent is not the same as empty.
    """
    home = tmp_path / "home"
    monkeypatch.setenv(config_module.ENV_HOME, str(home))
    monkeypatch.setenv(registry_module.ENV_VAR, str(tmp_path / "registry.yaml"))
    monkeypatch.setenv("LATENT_INTEL_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.delenv("LATENT_INTEL_PROJECT", raising=False)
    registry_module.clear_cache()
    from latent_intel import env as env_module

    env_module.reset()  # the list of loaded `.env` files is process-global
    return tmp_path
