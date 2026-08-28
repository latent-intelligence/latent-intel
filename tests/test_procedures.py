"""Declarative commands: what a project may express, and what it deliberately may not.

The boundary is `compile()`'s return type. A procedure that wanted to do more than
`list[Command]` would have to change that signature — which is the argument happening in
the open, rather than a feature arriving quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from latent_intel import procedures
from latent_intel.commands import Ask, Find

FIXTURE = Path(__file__).parent / "fixtures" / "projects" / "research" / "commands"


def test_a_search_compiles_to_one_find() -> None:
    found, problems = procedures.discover(FIXTURE)
    assert problems == []
    commands = procedures.compile(found["recent"], "compaction")

    assert len(commands) == 1
    assert isinstance(commands[0], Find)
    assert commands[0].query == "compaction" and commands[0].limit == 5


def test_a_prompt_compiles_to_one_ask_with_the_argument_substituted() -> None:
    found, _ = procedures.discover(FIXTURE)
    commands = procedures.compile(found["brief"], "nitrate levels")

    assert len(commands) == 1
    assert isinstance(commands[0], Ask)
    assert "nitrate levels" in commands[0].prompt
    assert "{{argument}}" not in commands[0].prompt


def test_a_sequence_is_a_batch_not_a_pipeline(tmp_path: Path) -> None:
    """Step N cannot see step N-1's result. Stated as a test so the limit is visible."""
    (tmp_path / "one.md").write_text(
        "---\nname: one\nkind: search\nquery: '{{argument}}'\n---\n", encoding="utf-8"
    )
    (tmp_path / "both.md").write_text(
        "---\nname: both\nkind: sequence\nsteps: [one, one]\n---\n", encoding="utf-8"
    )
    found, _ = procedures.discover(tmp_path)
    commands = procedures.compile(found["both"], "x", among=found)

    assert len(commands) == 2
    assert all(isinstance(c, Find) for c in commands)
    # Both received the same argument — nothing flowed between them.
    assert {c.query for c in commands} == {"x"}  # type: ignore[union-attr]


def test_a_sequence_cannot_include_itself(tmp_path: Path) -> None:
    (tmp_path / "loop.md").write_text(
        "---\nname: loop\nkind: sequence\nsteps: [loop]\n---\n", encoding="utf-8"
    )
    found, _ = procedures.discover(tmp_path)
    with pytest.raises(procedures.ProcedureError, match="itself"):
        procedures.compile(found["loop"], among=found)


def test_a_broken_file_is_reported_and_the_others_survive(tmp_path: Path) -> None:
    """One bad file must not take the tool down — the rule a broken plugin gets."""
    (tmp_path / "good.md").write_text(
        "---\nname: good\nkind: search\nquery: x\n---\n", encoding="utf-8"
    )
    (tmp_path / "nofrontmatter.md").write_text("just prose\n", encoding="utf-8")
    (tmp_path / "badkind.md").write_text(
        "---\nname: bad\nkind: teleport\n---\n", encoding="utf-8"
    )

    found, problems = procedures.discover(tmp_path)

    assert list(found) == ["good"]
    assert len(problems) == 2


def test_an_unknown_kind_names_what_is_allowed() -> None:
    with pytest.raises(procedures.ProcedureError, match="search, prompt, sequence"):
        procedures.parse("---\nname: x\nkind: teleport\n---\n", "x")


def test_a_missing_required_argument_is_refused() -> None:
    procedure = procedures.parse(
        "---\nname: x\nkind: prompt\nargument: topic\n"
        "argument_required: true\n---\nq\n",
        "x",
    )
    with pytest.raises(procedures.ProcedureError, match="topic"):
        procedures.compile(procedure, "")


def test_a_procedure_compiles_only_to_commands_the_session_already_understands() -> (
    None
):
    """The boundary, asserted. If this ever fails, a procedure has grown a capability
    and belongs behind an entry point instead."""
    found, _ = procedures.discover(FIXTURE)
    for procedure in found.values():
        for command in procedures.compile(procedure, "x", among=found):
            assert isinstance(command, Find | Ask)
