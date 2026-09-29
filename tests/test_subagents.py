"""Subagents: reading `agents/<name>.md` in Claude Code's format, and every refusal."""

from __future__ import annotations

from pathlib import Path

from latent_intel import subagents
from latent_intel.subagents import Subagent


def write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(text.encode("utf-8"))


def test_a_file_in_claude_code_s_format_is_a_subagent(tmp_path: Path) -> None:
    write(
        tmp_path,
        "reviewer.md",
        "---\nname: reviewer\ndescription: Checks a draft\n  against its sources.\n"
        "tools: design.search, WebSearch\nmodel: haiku\n---\n\nCheck it.\n",
    )
    problems: list[str] = []
    [agent] = subagents.load(tmp_path, problems)
    assert agent == Subagent(
        name="reviewer",
        description="Checks a draft against its sources.",
        prompt="Check it.",
        tools=("design.search", "WebSearch"),
        model="haiku",
    )
    assert problems == []


def test_tools_may_be_a_list_and_absent_means_inherit(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "---\nname: a\ndescription: d\ntools: [x.y]\n---\nP\n")
    write(tmp_path, "b.md", "---\nname: b\ndescription: d\n---\nP\n")
    write(tmp_path, "c.md", "---\nname: c\ndescription: d\ntools: []\n---\nP\n")
    found = {agent.name: agent.tools for agent in subagents.load(tmp_path, [])}
    assert found == {"a": ("x.y",), "b": None, "c": ()}


def test_an_incomplete_file_is_a_problem_and_skipped(tmp_path: Path) -> None:
    write(tmp_path, "bare.md", "Just prose.\n")
    write(tmp_path, "nameless.md", "---\ndescription: d\n---\nP\n")
    write(tmp_path, "silent.md", "---\nname: s\ndescription: d\n---\n\n")
    write(tmp_path, "badtools.md", "---\nname: t\ndescription: d\ntools: 3\n---\nP\n")
    write(tmp_path, "list.md", "---\n- one\n---\nP\n")
    problems: list[str] = []
    assert subagents.load(tmp_path, problems) == []
    text = "\n".join(problems)
    assert "bare.md: no frontmatter" in text
    assert "nameless.md: needs name" in text
    assert "silent.md: needs a body" in text
    assert "badtools.md: `tools` and `disallowedTools` must be lists" in text
    assert "list.md: frontmatter must be a mapping" in text


def test_a_second_file_with_the_same_name_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "---\nname: dup\ndescription: first\n---\nP\n")
    write(tmp_path, "b.md", "---\nname: dup\ndescription: second\n---\nP\n")
    problems: list[str] = []
    [agent] = subagents.load(tmp_path, problems)
    assert agent.description == "first"
    assert any("declared twice" in problem for problem in problems)


def test_keys_this_build_does_not_read_are_reported_not_honoured(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "a.md",
        "---\nname: a\ndescription: d\nmaxTurns: 99\ncolor: blue\n---\nP\n",
    )
    problems: list[str] = []
    [agent] = subagents.load(tmp_path, problems)
    assert agent.name == "a"
    assert any("`maxTurns` is not read" in problem for problem in problems)
    assert any("`color` is not read" in problem for problem in problems)


def test_a_key_that_could_only_narrow_it_refuses_the_agent(tmp_path: Path) -> None:
    """Ignoring `permissionMode` would hand the delegate more than its file allows;
    the runtime's own policy decides, so the file is refused rather than widened."""
    write(
        tmp_path, "a.md", "---\nname: a\ndescription: d\npermissionMode: plan\n---\nP\n"
    )
    problems: list[str] = []
    assert subagents.load(tmp_path, problems) == []
    assert problems == [
        "agents: a.md: `permissionMode` cannot be honoured here — the runtime's own "
        "permission policy decides; remove it — skipped"
    ]


def test_disallowed_tools_are_read_as_tools_are(tmp_path: Path) -> None:
    write(
        tmp_path,
        "a.md",
        "---\nname: a\ndescription: d\ndisallowedTools: WebFetch, design.delete\n"
        "---\nP\n",
    )
    [agent] = subagents.load(tmp_path, [])
    assert agent.tools is None
    assert agent.disallowed == ("WebFetch", "design.delete")


def test_an_unreadable_file_is_named_without_its_path(tmp_path: Path) -> None:
    """The path can carry an account's address — a synced drive's mount point — and
    `doctor` output is meant to be safe to paste."""
    (tmp_path / "odd.md").mkdir()
    problems: list[str] = []
    assert subagents.load(tmp_path, problems) == []
    assert problems == ["agents: odd.md: Is a directory"]
    assert str(tmp_path) not in problems[0]


def test_empty_frontmatter_reports_what_is_missing(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "---\n# nothing yet\n---\nP\n")
    problems: list[str] = []
    assert subagents.load(tmp_path, problems) == []
    assert problems == ["agents: a.md: needs name, description — skipped"]


def test_an_unreadable_file_costs_only_itself(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "a.md").write_bytes(b"---\nname: a\ndescription: \xff\n---\nP\n")
    write(tmp_path, "b.md", "---\nname: b\ndescription: d\n---\nP\n")
    problems: list[str] = []
    assert [agent.name for agent in subagents.load(tmp_path, problems)] == ["b"]
    assert len(problems) == 1


def test_no_directory_is_no_subagents() -> None:
    assert subagents.load(None, []) == []
