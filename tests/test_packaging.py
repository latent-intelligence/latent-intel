"""What ships. Only `latent-intel` goes to the public repo, so its metadata may not
depend on a sibling that is not published alongside it.

This is a real regression, not a hypothetical: `[tool.uv.sources]` pinned
`../latent-wiki` and `../latent-records` by relative path. `uv tool install .`
still worked, which is why it went unnoticed — but `uv tool install '.[wiki]'`
and *every* `uv sync` failed on any clone without the siblings beside it, taking
`just check` with them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"
SIBLINGS = ("latent-wiki", "latent-records")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_no_dependency_is_a_sibling_package() -> None:
    data = _pyproject()
    declared = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        declared += extra

    for requirement in declared:
        for sibling in SIBLINGS:
            assert sibling not in requirement, (
                f"{requirement!r} depends on {sibling}, which is not published. "
                "Add it by path instead: uv tool install . --with ../latent-wiki"
            )


def test_no_dependency_is_pinned_to_a_local_path() -> None:
    """A relative path pin resolves against the *clone*, so it works on exactly one
    machine and silently poisons the lockfile for everyone else."""
    sources = _pyproject().get("tool", {}).get("uv", {}).get("sources", {})
    assert sources == {}, f"local path pins would not resolve elsewhere: {sources}"


def test_the_lockfile_is_resolvable_without_siblings() -> None:
    lock = (PYPROJECT.parent / "uv.lock").read_text(encoding="utf-8")
    for sibling in SIBLINGS:
        assert sibling not in lock, (
            f"uv.lock still references {sibling}; run `uv lock` after removing it, "
            "or no clone without the siblings can `uv sync`."
        )
