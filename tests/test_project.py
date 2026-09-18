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


def test_a_relative_cwd_resolves_against_the_project_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working directory is a location, so it gets `target:`'s rule — a project
    cloned anywhere must start its server in the directory the file meant."""
    home = tmp_path / "elsewhere"
    (home / "server").mkdir(parents=True)
    path = write(
        home,
        "served",
        "sources:\n"
        "  - {id: records, kind: mcp, target: records-mcp, options: {cwd: ./server}}\n",
    )

    monkeypatch.chdir(tmp_path)
    source = project.load(path).sources[0]

    assert source.options["cwd"] == str(home / "server")
    # This row always had the shape that broke; only `cwd` was ever asserted on.
    assert source.target == "records-mcp"


def test_an_mcp_target_is_a_command_line_and_is_never_path_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Joining a command to the project directory produced a path nothing could start.

    A console script became `<project dir>/example-mcp`, and a multi-word command
    became one Path with spaces in it — on every platform, not just the Windows one
    where it was reported. Variables still substitute, because a project that names its
    server through `vars:` is the same portability argument as everywhere else.
    """
    home = tmp_path / "elsewhere"
    path = write(
        home,
        "served",
        "vars: {SERVER: example-mcp}\n"
        "sources:\n"
        "  - {id: bare, kind: mcp, target: example-mcp}\n"
        '  - {id: words, kind: mcp, target: "npx -y @example/mcp"}\n'
        '  - {id: var, kind: mcp, target: "${SERVER} --stdio"}\n',
    )

    monkeypatch.chdir(tmp_path)
    bare, words, var = project.load(path).sources

    assert bare.target == "example-mcp"
    assert words.target == "npx -y @example/mcp"
    assert var.target == "example-mcp --stdio"


# -- the persona and the skills ---------------------------------------------


def test_a_persona_is_read_from_a_path_resolved_against_the_project_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persona is a path like every other, so it obeys the path rule: a voice that
    only resolves where it was authored is the failure that rule exists for."""
    home = tmp_path / "elsewhere"
    home.mkdir()
    (home / "persona.md").write_text("You are a careful archivist.\n", encoding="utf-8")
    path = write(home, "voiced", "agent: {persona: ./persona.md}\n")

    monkeypatch.chdir(tmp_path)
    loaded = project.load(path)

    assert loaded.persona == "You are a careful archivist."
    assert loaded.persona_mode == "append"
    assert loaded.problems == []


def test_a_persona_file_that_is_not_there_is_reported_not_fatal(tmp_path: Path) -> None:
    """One missing file must not cost a deployment its sources, its commands and its
    branding — the same non-fatal discipline a bad source gets."""
    path = write(tmp_path, "absent", "agent: {persona: ./nowhere.md}\n")
    loaded = project.load(path)

    assert loaded.persona == ""
    assert len(loaded.problems) == 1 and "persona" in loaded.problems[0]


def test_a_mode_this_build_does_not_know_is_named_and_read_as_append(
    tmp_path: Path,
) -> None:
    """A misspelled mode is worth saying out loud, and worth nothing else: the
    deployment still sounds like itself."""
    path = write(tmp_path, "typo", "agent: {persona_mode: replce}\n")
    loaded = project.load(path)

    assert loaded.persona_mode == "append"
    assert any("replce" in problem for problem in loaded.problems), loaded.problems


def test_skills_load_in_filename_order_and_name_themselves(tmp_path: Path) -> None:
    """A skill is prose. Frontmatter names it when the author wants a name other than
    the filename, and is not required for a file to be a skill at all."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "b-plain.md").write_text("No frontmatter here.\n", encoding="utf-8")
    (skills / "a-named.md").write_text(
        "---\nname: citation-style\n---\nCite inline.\n", encoding="utf-8"
    )
    path = write(tmp_path, "skilled", "skills: ./skills\n")

    loaded = project.load(path)

    assert loaded.skills == [
        ("citation-style", "Cite inline."),
        ("b-plain", "No frontmatter here."),
    ]
    assert loaded.problems == []


def test_one_unreadable_skill_is_skipped_while_the_others_still_load(
    tmp_path: Path,
) -> None:
    """One bad file must not cost a deployment its other four."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "good.md").write_text("Fine.\n", encoding="utf-8")
    (skills / "broken.md").write_text(
        "---\nname: [unclosed\n---\nbody\n", encoding="utf-8"
    )
    (skills / "later.md").write_text("Also fine.\n", encoding="utf-8")
    path = write(tmp_path, "partial", "skills: ./skills\n")

    loaded = project.load(path)

    assert [name for name, _ in loaded.skills] == ["good", "later"]
    assert len(loaded.problems) == 1 and "broken.md" in loaded.problems[0]


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
