"""Isolation every test gets, whether it asks for one or not.

Before this file, only `LATENT_INTEL_HOME` was isolated, and only for `test_cli.py`.
`KNOWLEDGE_REGISTRY` was isolated nowhere — so a test on a developer machine could see
seven real store ids through `Session.available()`, pass here, and fail anywhere else.
A test that reads the machine it runs on is not a test.

Autouse and session-wide: an opt-in fixture is one someone forgets.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from latent_intel import config as config_module
from latent_intel import registry as registry_module

#: Everything a runtime reads a credential from. Deleted wholesale rather than named
#: one by one: a developer with Foundry exported would otherwise see `anthropic`
#: available in a test asserting it is not, and the failure would only appear on someone
#: else's machine. Prefix-matched so a variable the SDK grows is covered the day it
#: arrives.
CREDENTIAL_PREFIXES = ("ANTHROPIC_", "LATENT_INTEL_ANTHROPIC_")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test reads or writes real state — config, data, projects, registry or keys.

    Each variable is pointed at a path under `tmp_path` rather than merely unset: an
    unset `KNOWLEDGE_REGISTRY` falls back to `DEFAULT_REGISTRY`, which is a real file on
    a developer machine. Absent is not the same as empty.
    """
    home = tmp_path / "home"
    monkeypatch.setenv(config_module.ENV_HOME, str(home))
    monkeypatch.setenv(registry_module.ENV_VAR, str(tmp_path / "registry.yaml"))
    monkeypatch.setenv("LATENT_INTEL_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.delenv("LATENT_INTEL_PROJECT", raising=False)
    for name in list(os.environ):
        if name.startswith(CREDENTIAL_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    registry_module.clear_cache()
    from latent_intel import env as env_module
    from latent_intel import settings as settings_module

    env_module.reset()  # the list of loaded `.env` files is process-global
    settings_module.invalidate()  # so is the resolved view
    return tmp_path
