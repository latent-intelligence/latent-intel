"""Projects: discovery, the path rule, and variables.

The path rule gets the most attention here because it is the one that fails silently and
late — a project authored on one machine that only resolves on that machine, discovered
by a client rather than by us.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from latent_intel import project


def write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# -- the path rule ----------------------------------------------------------


def test_relative_paths_resolve_against_the_project_file_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this rule exists to prevent: a project that works only where authored."""
    home = tmp_path / "elsewhere"
    (home / "corpus").mkdir(parents=True)
    (home / "commands").mkdir()
    path = write(
        home,
        "portable",
        "sources:\n"
        "  - {id: docs, kind: files, target: ./corpus}\n"
        "commands: ./commands\n",
    )

    monkeypatch.chdir(tmp_path)  # deliberately *not* the project's directory
    loaded = project.load(path)

    assert loaded.sources[0].target == str(home / "corpus")
    assert loaded.commands_dir == home / "commands"
    assert loaded.problems == []


def test_a_uri_target_is_left_alone(tmp_path: Path) -> None:
    """`s3://bucket/key` is already absolute. Joining it to a directory corrupts it —
    the same collapse this codebase has shipped four times in other guises."""
    path = write(
        tmp_path,
        "remote",
        "sources:\n  - {id: mirror, kind: files, target: 's3://bucket/li/raw'}\n",
    )
    assert project.load(path).sources[0].target == "s3://bucket/li/raw"


def test_an_absolute_target_survives_unchanged(tmp_path: Path) -> None:
    path = write(
        tmp_path, "abs", f"sources:\n  - {{id: d, kind: files, target: {tmp_path}}}\n"
    )
    assert project.load(path).sources[0].target == str(tmp_path)


# -- local and remote -------------------------------------------------------


def test_a_registry_name_and_a_literal_target_are_different_things(
    tmp_path: Path,
) -> None:
    """`from:` re-resolves per machine; `target:` is taken as given. `remote: true`
    selects a registry entry's mirror, which is a third thing again."""
    path = write(
        tmp_path,
        "mixed",
        "sources:\n"
        "  - {id: byname, kind: wiki, from: design-wiki, remote: true}\n"
        "  - {id: literal, kind: files, target: 's3://bucket/x'}\n",
    )
    byname, literal = project.load(path).sources

    assert byname.by_name and byname.spec == "design-wiki" and byname.remote
    assert not literal.by_name and literal.spec == "s3://bucket/x"


# -- variables --------------------------------------------------------------


def test_the_environment_beats_a_committed_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How one project file serves a laptop and a deployment host, unedited."""
    monkeypatch.setenv("RESEARCH_MIRROR", "s3://deployed/li")
    path = write(
        tmp_path,
        "vars",
        "vars: {RESEARCH_MIRROR: 's3://committed/li'}\n"
        "sources:\n  - {id: d, kind: files, target: '${RESEARCH_MIRROR}/raw'}\n",
    )
    assert project.load(path).sources[0].target == "s3://deployed/li/raw"


def test_an_unset_variable_is_a_named_problem_not_a_local_path(
    tmp_path: Path,
) -> None:
    """An unresolved `${VAR}` used to stay in the string, which the path rule then
    joined to the project directory: `<project>/${LI_S3}/wikis/sbm`, a local path that
    exists nowhere, reported much later as a store with no manifest. The source is
    skipped and the variable named, so the fix is one line in `vars:` or one export."""
    path = write(
        tmp_path,
        "unset",
        "sources:\n"
        "  - {id: d, kind: wiki, target: '${NOT_SET}/wikis/d'}\n"
        "  - {id: ok, kind: files, target: 's3://bucket/raw'}\n",
    )
    loaded = project.load(path)
    assert [s.id for s in loaded.sources] == ["ok"]
    assert any("NOT_SET" in p and "'d'" in p for p in loaded.problems), loaded.problems


def test_options_are_substituted_like_targets(tmp_path: Path) -> None:
    """`context:` beside a wiki is a location too. Honouring `${LI_S3}` in `target:`
    and passing it through literally one line below it was the quiet kind of wrong."""
    path = write(
        tmp_path,
        "opts",
        "vars: {LI_S3: 's3://bucket/li'}\n"
        "sources:\n"
        "  - id: w\n"
        "    kind: wiki\n"
        "    target: '${LI_S3}/wikis/w'\n"
        "    options: {context: '${LI_S3}/context/w', limit: 8}\n",
    )
    source = project.load(path).sources[0]
    assert source.options == {"context": "s3://bucket/li/context/w", "limit": 8}


# -- discovery --------------------------------------------------------------


def test_discovery_prefers_the_earlier_search_path_and_reports_the_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    write(first, "research", "title: winner\n")
    write(second, "research", "title: loser\n")
    monkeypatch.setenv(project.ENV_PROJECTS, os.pathsep.join([str(first), str(second)]))

    found, collisions = project.discover()

    assert project.load(found["research"]).title == "winner"
    assert len(collisions) == 1 and "research" in collisions[0]


def test_the_shipped_project_is_always_discoverable(tmp_path: Path) -> None:
    """Our own branding takes the client path, so the client path cannot rot."""
    found, _ = project.discover(home=tmp_path)
    assert "latent" in found
    assert project.load(found["latent"]).branding["name"] == "LATENT"


def test_the_shipped_project_declares_no_sources() -> None:
    """A tool that arrives pointed at something has decided for you."""
    found, _ = project.discover()
    assert project.load(found["latent"]).sources == []


# -- validation is non-fatal ------------------------------------------------


def test_a_bad_source_is_reported_and_the_others_survive(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "partial",
        "sources:\n"
        "  - {kind: files, target: /tmp}\n"  # no id
        "  - {id: nowhere, kind: files}\n"  # neither from nor target
        "  - {id: good, kind: files, target: /tmp}\n",
    )
    loaded = project.load(path)

    assert [s.id for s in loaded.sources] == ["good"]
    assert len(loaded.problems) == 2


def test_a_reserved_name_is_refused(tmp_path: Path) -> None:
    assert "reserved" in project.load(write(tmp_path, "none", "title: x\n")).problems[0]


def test_a_file_that_is_not_a_mapping_raises(tmp_path: Path) -> None:
    with pytest.raises(project.ProjectError):
        project.load(write(tmp_path, "bad", "- just\n- a list\n"))
