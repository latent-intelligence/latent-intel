"""`.env` as an override on the ambient environment, never on the real one.

Two rules carry the whole design, and both are easy to get backwards:
an exported variable always wins, and a value never reaches a command line.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from latent_intel import env, serve


@pytest.fixture(autouse=True)
def _forget() -> None:
    env.reset()


def write(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / env.FILENAME
    path.write_text(body, encoding="utf-8")
    return path


# -- precedence ---------------------------------------------------------------


def test_an_exported_variable_beats_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule `project.substitute` already follows. One precedence rule, not two —
    and it keeps `AWS_PROFILE=other intel …` working as a deliberate one-off."""
    monkeypatch.setenv("AWS_PROFILE", "exported")
    write(tmp_path, "AWS_PROFILE=from_file\n")

    env.load(tmp_path, None)

    assert os.environ["AWS_PROFILE"] == "exported"


def test_the_file_fills_a_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    write(tmp_path, "AWS_PROFILE=from_file\n")

    env.load(tmp_path, None)

    assert os.environ["AWS_PROFILE"] == "from_file"


def test_the_project_file_beats_the_machine_wide_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentials belong to the deployment that needs them."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("SHARED", raising=False)
    write(tmp_path / "project", "AWS_PROFILE=project\n")
    write(tmp_path / "home", "AWS_PROFILE=home\nSHARED=home\n")

    env.load(tmp_path / "project", tmp_path / "home")

    assert os.environ["AWS_PROFILE"] == "project"
    # The farther file still fills what the nearer one does not mention.
    assert os.environ["SHARED"] == "home"


def test_the_working_directory_is_never_consulted(tmp_path: Path) -> None:
    """A project is named and switched, never inferred from where you stand."""
    write(tmp_path, "SHOULD_NOT_LOAD=1\n")

    assert env.candidates(None, None) == []


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert env.load(tmp_path / "absent", None) == []


# -- what crosses the process boundary ---------------------------------------


def test_a_subprocess_is_told_the_path_never_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP block is passed to `claude` as an argument, so a secret placed in it
    would be visible in `ps`. The child re-reads the file instead."""
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    path = write(tmp_path, "AWS_SECRET_ACCESS_KEY=super-secret\n")
    env.load(tmp_path, None)

    spec = serve.launch_spec("files", str(tmp_path), "notes", {})
    argv = " ".join(spec["args"])

    assert "super-secret" not in argv
    assert str(path) in argv
    assert spec["args"][spec["args"].index("--env-file") + 1] == str(path)


def test_the_child_resolves_credentials_exactly_as_the_parent_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`serve` shares `apply`, so a child cannot drift from its parent's precedence.

    It matters because MCP passes only HOME, LOGNAME, PATH, SHELL, TERM and USER to a
    server subprocess — every credential the parent held is otherwise gone.
    """
    monkeypatch.setenv("KEPT", "exported")
    monkeypatch.delenv("FILLED", raising=False)
    path = write(tmp_path, "KEPT=from_file\nFILLED=from_file\n")

    env.apply([path])

    assert os.environ["KEPT"] == "exported"
    assert os.environ["FILLED"] == "from_file"


def test_no_env_file_means_no_flag(tmp_path: Path) -> None:
    env.reset()
    spec = serve.launch_spec("files", str(tmp_path), "notes", {})

    assert "--env-file" not in spec["args"]


# -- credentials must not escape ----------------------------------------------


def test_a_dotenv_is_ignored_at_every_depth() -> None:
    """A deployment directory can live anywhere, including inside a checkout. The
    ignore rule must not be anchored to the repo root."""
    rules = (Path(__file__).parent.parent / ".gitignore").read_text().splitlines()
    stripped = [line.strip() for line in rules]

    assert ".env" in stripped, ".env must be ignored"
    assert "/.env" not in stripped, "an anchored rule would miss a nested deployment"


def test_no_module_emits_an_environment_value() -> None:
    """Credentials are read, never printed. A traceback or a debug line carrying a
    secret is the same leak as committing one."""
    source = Path(__file__).parent.parent / "src" / "latent_intel"
    emitters = ("print(", "console.print", "log.info", "log.debug", "logger.")

    for path in source.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "os.environ" in line and any(e in line for e in emitters):
                raise AssertionError(f"{path.name}:{number} emits an env value: {line}")
