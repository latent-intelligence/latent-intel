"""`intel serve --deployment X`: a whole project over MCP, from the project file.

The fixture project points at the published-format samples (a wiki and its context
store), carries a persona, one skill, two saved commands and a tree view — every
piece a deployment declares and the server turns into something a client can reach.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from latent_intel import project as project_module
from latent_intel import serve_project, serve_skills

pytestmark = pytest.mark.anyio

FORMATS = Path(__file__).resolve().parent / "fixtures" / "formats"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    shutil.copytree(FORMATS / "wiki-store", tmp_path / "wiki")
    shutil.copytree(FORMATS / "context-store", tmp_path / "context")
    manifest = tmp_path / "wiki" / "manifest.json"
    data = json.loads(manifest.read_text())
    for entry in data["pages"]:
        if entry["key"] == "count-limpets":
            entry["frontmatter"]["part_of"] = "tide-pool"
    manifest.write_text(json.dumps(data))

    (tmp_path / "persona.md").write_text("You answer for the Heronsgate survey team.\n")
    (tmp_path / "skills" / "walk-a-procedure").mkdir(parents=True)
    (tmp_path / "skills" / "walk-a-procedure" / "SKILL.md").write_text(
        "---\nname: walk-a-procedure\ndescription: Walk a procedure step by step.\n"
        "---\nOpen the view, then each step in order. Focus: $ARGUMENTS\n"
    )
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "evidence.md").write_text(
        "---\nkind: search\nquery: '{{argument}}'\nsource: heron\nlimit: 5\n"
        "description: Find the evidence for a topic.\n---\n"
    )
    (tmp_path / "commands" / "brief.md").write_text(
        "---\nkind: prompt\nargument: topic\n---\nBrief me on {{argument}}.\n"
    )
    (tmp_path / "heronsgate.yaml").write_text(
        """\
schema_version: 1
title: Heronsgate
description: The fixture survey.
agent: {persona: persona.md}
skills: skills
commands: commands
sources:
  - {id: heron, kind: wiki, target: wiki, options: {context: context}}
  - {id: gauges, kind: mcp, target: "gauge-mcp --readonly"}
serve:
  auth: {token_env: HERON_TOKEN}
views:
  - {name: steps, kind: tree, root_type: concept, parent: part_of, order: step,
     fields: [aliases]}
"""
    )
    return tmp_path / "heronsgate.yaml"


async def served(path: Path) -> tuple[Any, serve_project.Served, Any]:
    project = project_module.load(path)
    assert project.problems == []
    sources = await serve_project.open_sources(project)
    return project, sources, serve_project.build(project, sources)


# -- what is served -----------------------------------------------------------


async def test_project_server_lists_ladder_tools(deployment: Path) -> None:
    _, _, server = await served(deployment)
    names = {tool.name for tool in await server.list_tools()}
    assert {
        "wiki_overview",
        "wiki_index",
        "wiki_search",
        "wiki_get",
        "wiki_neighbors",
        "wiki_trace",
        "wiki_evidence",
        "wiki_raw",
        "wiki_view",
        "load_skill",
    } <= names
    assert all(t.annotations.read_only_hint for t in await server.list_tools())


async def test_a_third_party_mcp_source_is_skipped_and_named(deployment: Path) -> None:
    _, sources, _ = await served(deployment)
    assert set(sources.connectors) == {"heron"}
    assert any(line.startswith("gauges:") for line in sources.skipped)


async def test_persona_becomes_instructions(deployment: Path) -> None:
    project, sources, _ = await served(deployment)
    text = serve_project.instructions(project, sources)
    assert "Heronsgate survey team" in text
    assert "start with wiki_overview" in text
    assert "walk-a-procedure: Walk a procedure step by step." in text
    assert "steps" in text


async def test_a_tool_call_reaches_the_connector(deployment: Path) -> None:
    _, _, server = await served(deployment)
    result = await server.call_tool("wiki_search", {"query": "limpets"})
    assert "count-limpets" in str(result)


# -- prompts and skills -------------------------------------------------------


async def test_skills_and_commands_become_prompts(deployment: Path) -> None:
    _, _, server = await served(deployment)
    names = {p.name for p in await server.list_prompts()}
    assert {"walk-a-procedure", "evidence", "brief"} <= names
    brief = await server.get_prompt("brief", {"argument": "limpets"})
    assert "Brief me on limpets." in str(brief)
    search = await server.get_prompt("evidence", {"argument": "pools"})
    assert "Search in heron for 'pools'" in str(search)
    skill = await server.get_prompt("walk-a-procedure", {"arguments": "counting"})
    assert "Focus: counting" in str(skill)


async def test_skills_extension_serves_skill_resources(deployment: Path) -> None:
    project, _, server = await served(deployment)
    uris = {str(r.uri) for r in await server.list_resources()}
    assert "skill://walk-a-procedure/SKILL.md" in uris
    extension = serve_skills.SkillsExtension(project.skills)
    assert extension.identifier == "io.modelcontextprotocol/skills"
    assert extension.entries()[0]["uri"] == "skill://walk-a-procedure/SKILL.md"
    assert (
        "each step in order"
        in extension.get("skill://walk-a-procedure/SKILL.md")["text"]
    )
    with pytest.raises(ValueError, match="served:"):
        extension.get("skill://nope/SKILL.md")
    assert {m.method for m in extension.methods()} == {"skills/list", "skills/get"}


async def test_load_skill_fallback_is_present(deployment: Path) -> None:
    _, _, server = await served(deployment)
    result = await server.call_tool("load_skill", {"name": "walk-a-procedure"})
    assert "each step in order" in str(result)


# -- views --------------------------------------------------------------------


async def test_view_tool_renders_the_tree(deployment: Path) -> None:
    _, _, server = await served(deployment)
    text = str(await server.call_tool("wiki_view", {"name": "steps"}))
    assert "Tide pool (tide-pool)" in text
    assert "1. Count limpets (count-limpets) · draft" in text
    assert "aliases: rock pool" in text


async def test_view_reports_orphans(tmp_path: Path) -> None:
    from latent_intel import views
    from latent_intel.project import View

    view = View(name="v", kind="tree", root_type="flow", parent="part_of")
    tree = views.build(
        view,
        {
            "a": ("A", {"type": "flow"}),
            "b": ("B", {"type": "task", "part_of": "missing"}),
        },
    )
    text = views.render(tree)
    assert "orphans (parent not a page): b" in text
    assert "named but not broken down: a" in text


async def test_view_resource_is_data(deployment: Path) -> None:
    _, _, server = await served(deployment)
    contents = await server.read_resource("view://steps")
    payload = json.loads(next(iter(contents)).content)
    assert payload["roots"][0]["children"][0]["key"] == "count-limpets"


# -- transport ----------------------------------------------------------------


async def _call(app: Any, headers: list[tuple[bytes, bytes]]) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    await app(scope, receive, send)
    return int(sent[0]["status"])


async def test_http_rejects_a_missing_or_wrong_bearer() -> None:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = serve_project.BearerCheck(inner, "s3cret")
    assert await _call(app, []) == 401
    assert await _call(app, [(b"authorization", b"Bearer wrong")]) == 401
    assert await _call(app, [(b"authorization", b"Bearer s3cret")]) == 200


async def test_http_without_a_token_is_refused(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERON_TOKEN", raising=False)
    project = project_module.load(deployment)
    with pytest.raises(serve_project.ServeError, match="HERON_TOKEN"):
        await serve_project.run(project, transport="http", host="0.0.0.0", port=0)


def test_client_config_stdio_and_http(deployment: Path) -> None:
    project = project_module.load(deployment)
    stdio = serve_project.client_config(
        project, transport="stdio", host="", port=0, command=["uvx", "intel"]
    )
    ours = stdio["mcpServers"]["intel-heronsgate"]
    assert ours["command"] == "uvx"
    assert ours["args"][-3:] == ["serve", "--deployment", str(project.path)]
    assert stdio["mcpServers"]["gauges"] == {
        "command": "gauge-mcp",
        "args": ["--readonly"],
    }
    http = serve_project.client_config(
        project, transport="http", host="127.0.0.1", port=8765
    )
    entry = http["mcpServers"]["intel-heronsgate"]
    assert entry["url"] == "http://127.0.0.1:8765/mcp"
    assert entry["headers"] == {"Authorization": "Bearer ${HERON_TOKEN}"}


async def test_an_omitted_argument_means_what_it_means_in_process(
    deployment: Path,
) -> None:
    """Over MCP an omitted argument arrives as the schema default, so each one that is
    not the empty value is declared — or drafts vanish and limit becomes one."""
    _, sources, server = await served(deployment)
    result = await server.call_tool("wiki_search", {"query": "pool"})
    over_mcp = result.content[0].text
    in_process = await sources.connectors["heron"].call(
        "wiki_search", {"query": "pool"}
    )
    assert in_process in over_mcp
    assert "count-limpets" in over_mcp


# -- the CLI ------------------------------------------------------------------


def test_cli_serve_print_config_stdio(deployment: Path) -> None:
    from typer.testing import CliRunner

    from latent_intel.frontends.cli.main import app

    result = CliRunner().invoke(
        app, ["serve", "--deployment", str(deployment), "--print-config"]
    )
    assert result.exit_code == 0, result.output
    block = json.loads(result.stdout)
    assert block["mcpServers"]["intel-heronsgate"]["command"] == "intel"


def test_cli_serve_print_config_http_has_no_literal_token(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from latent_intel.frontends.cli.main import app

    monkeypatch.setenv("HERON_TOKEN", "do-not-print-me")
    result = CliRunner().invoke(
        app,
        ["serve", "-d", str(deployment), "--transport", "http", "--print-config"],
    )
    assert result.exit_code == 0, result.output
    assert "do-not-print-me" not in result.output
    assert "${HERON_TOKEN}" in result.output


def test_cli_serve_names_an_unknown_deployment(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from latent_intel.frontends.cli.main import app

    result = CliRunner().invoke(app, ["serve", "-d", "no-such-deployment"])
    assert result.exit_code == 1
    assert "no deployment 'no-such-deployment'" in result.output


# -- review regressions -------------------------------------------------------


async def test_a_registry_source_that_is_an_mcp_server_is_skipped(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from latent_intel import registry

    text = deployment.read_text().replace(
        '  - {id: gauges, kind: mcp, target: "gauge-mcp --readonly"}\n',
        "  - {id: tracker, from: tracker}\n",
    )
    deployment.write_text(text)
    monkeypatch.setattr(
        registry,
        "resolve",
        lambda name, explicit=None, remote=False: ("mcp", "tracker-mcp"),
    )
    project = project_module.load(deployment)
    sources = await serve_project.open_sources(project)
    assert "tracker" not in sources.connectors
    assert any(line.startswith("tracker:") for line in sources.skipped)
    config = serve_project.client_config(project, transport="stdio", host="", port=0)
    assert config["mcpServers"]["tracker"] == {"command": "tracker-mcp", "args": []}


async def test_with_several_sources_instructions_name_the_prefixed_tools(
    deployment: Path, tmp_path: Path
) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("# A\n\nA note.\n")
    deployment.write_text(
        deployment.read_text().replace(
            "sources:\n",
            f"sources:\n  - {{id: notes, kind: files, target: {tmp_path / 'notes'}}}\n",
        )
    )
    project, sources, server = await served(deployment)
    names = {t.name for t in await server.list_tools()}
    assert "heron__wiki_overview" in names and "wiki_overview" not in names
    assert "start with heron__wiki_overview" in serve_project.instructions(
        project, sources
    )


async def test_a_view_resource_serialises_dates() -> None:
    import datetime

    from latent_intel import views
    from latent_intel.project import View

    view = View(
        name="v", kind="tree", root_type="flow", parent="part_of", fields=("captured",)
    )
    tree = views.build(
        view, {"a": ("A", {"type": "flow", "captured": datetime.date(2026, 10, 6)})}
    )
    payload = json.loads(serve_project._view_resource(tree)())
    assert payload["roots"][0]["captured"] == "2026-10-06"


async def test_mixed_sibling_orders_do_not_crash_and_cycles_are_reported() -> None:
    from latent_intel import views
    from latent_intel.project import View

    view = View(name="v", kind="tree", root_type="flow", parent="part_of", order="step")
    tree = views.build(
        view,
        {
            "root": ("Root", {"type": "flow"}),
            "a": ("A", {"type": "task", "part_of": "root", "step": "b"}),
            "b": ("B", {"type": "task", "part_of": "root", "step": 1}),
            "c": ("C", {"type": "task", "part_of": "root"}),
            "x": ("X", {"type": "note"}),
            "under-x": ("Under X", {"type": "task", "part_of": "x"}),
            "loop-1": ("L1", {"type": "task", "part_of": "loop-2"}),
            "loop-2": ("L2", {"type": "task", "part_of": "loop-1"}),
        },
    )
    assert [n.key for n in tree.roots[0].children] == ["b", "a", "c"]
    assert tree.unreached == ["loop-1", "loop-2", "under-x"]
    assert "not reached from any root: loop-1, loop-2, under-x" in views.render(tree)


async def test_a_required_command_argument_is_required_in_the_prompt(
    deployment: Path,
) -> None:
    (deployment.parent / "commands" / "dig.md").write_text(
        "---\nkind: prompt\nargument: topic\nargument_required: true\n---\n"
        "Dig into {{argument}}.\n"
    )
    _, _, server = await served(deployment)
    prompt = next(p for p in await server.list_prompts() if p.name == "dig")
    assert prompt.arguments and prompt.arguments[0].required is True


async def test_bearer_scheme_is_case_insensitive_and_websockets_are_refused() -> None:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = serve_project.BearerCheck(inner, "s3cret")
    assert await _call(app, [(b"authorization", b"bearer s3cret")]) == 200
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "websocket", "headers": []}, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_http_token_is_checked_before_sources_open(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERON_TOKEN", raising=False)
    opened: list[str] = []

    async def spy(project: Any) -> Any:
        opened.append(project.name)
        return serve_project.Served()

    monkeypatch.setattr(serve_project, "open_sources", spy)
    project = project_module.load(deployment)
    with pytest.raises(serve_project.ServeError):
        await serve_project.run(project, transport="http", host="0.0.0.0", port=0)
    assert opened == []


async def test_cleanup_closes_every_source_when_one_fails() -> None:
    closed: list[str] = []

    class Source:
        def __init__(self, name: str, fail: bool) -> None:
            self.name, self.fail = name, fail

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("boom")

    served_ = serve_project.Served(
        connectors={"a": Source("a", True), "b": Source("b", False)}
    )
    notes: list[str] = []
    await serve_project._close(served_, notes.append)
    assert closed == ["a", "b"] and notes == ["closing a: boom"]


def test_client_config_dials_a_usable_address(deployment: Path) -> None:
    project = project_module.load(deployment)
    ipv6 = serve_project.client_config(project, transport="http", host="::1", port=1)
    assert ipv6["mcpServers"]["intel-heronsgate"]["url"] == "http://[::1]:1/mcp"
    wild = serve_project.client_config(
        project, transport="http", host="0.0.0.0", port=1
    )
    assert wild["mcpServers"]["intel-heronsgate"]["url"] == "http://<host>:1/mcp"


def test_cli_serve_reports_a_broken_project_file(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from latent_intel.frontends.cli.main import app

    broken = tmp_path / "broken.yaml"
    broken.write_text("- just\n- a list\n")
    result = CliRunner().invoke(app, ["serve", "-d", str(broken)])
    assert result.exit_code == 1
    assert "must be a mapping" in result.output
