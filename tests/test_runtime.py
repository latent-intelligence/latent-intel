"""The runtime seam, and the `claude-cli` backend behind it.

The subprocess tests replay recorded `claude` output through a fake interpreter rather
than mocking `open_process`, so line splitting, stderr draining and exit codes are all
exercised for real. The recordings came from live runs and every trap they cover was
observed rather than imagined.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from latent_intel import events as ev
from latent_intel.agent import base as agent
from latent_intel.agent.base import WEB_REMEDY
from latent_intel.agent.runtimes import claude_cli
from latent_intel.agent.runtimes.claude_cli import (
    ClaudeCliRuntime,
    allowed_tools,
    build_argv,
    sanitise,
)
from latent_intel.models import (
    Effect,
    Message,
    RuntimeUnavailable,
    Skill,
    ToolSpec,
    WebScope,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def runtime(scenario: str, **options: object) -> ClaudeCliRuntime:
    return ClaudeCliRuntime(command=[sys.executable, str(FAKE), scenario], **options)


async def collect(
    scenario: str,
    tools: list[ToolSpec] | None = None,
    **options: object,
) -> list[ev.AgentEvent]:
    r = runtime(scenario)
    emitter = ev.Emitter(uuid4())
    return [
        event
        async for event in r.stream(
            [Message(text="q")], tools or [], emitter=emitter, **options
        )
    ]


# -- the seam ---------------------------------------------------------------


def test_our_own_runtime_is_registered_through_entry_points() -> None:
    """The extension path is the one we take ourselves, so it cannot rot unnoticed."""
    assert "claude-cli" in agent.available_kinds()


def test_an_unknown_kind_names_what_is_installed() -> None:
    with pytest.raises(RuntimeUnavailable, match="installed:"):
        agent.build("telepathy")


def test_the_in_process_runtimes_load_even_where_they_cannot_run() -> None:
    """Absent and unusable are different states. Each imports its SDK inside a turn, so
    it is listed — with a reason — on a machine with no credentials, rather than
    vanishing and taking the diagnosis with it."""
    assert "custom" in agent.available_kinds()
    assert "anthropic-sdk" in agent.available_kinds()
    assert "openai-agents" in agent.available_kinds()
    assert "claude-agent-sdk" in agent.available_kinds()


def test_each_extra_is_a_runtime_and_all_names_every_one() -> None:
    """The extra that fixes a runtime is the runtime's own name, so the remedy it prints
    and the thing to install are one word; and `[all]` names the others rather than
    repeating their packages, so an extra left out of it would install quietly less."""
    import tomllib

    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    runtimes = set(extras) - {"all", "dev"}
    assert runtimes <= set(agent.available_kinds())
    [everything] = extras["all"]
    assert set(everything.split("[", 1)[1].rstrip("]").split(",")) == runtimes


def test_a_config_key_this_runtime_does_not_know_is_rejected_by_name() -> None:
    """Ignoring it honoured a file that says the setting is on — a misspelled `model:`
    launched the binary on its own default. `agent.build` propagates the refusal, so
    `/runtime` and `doctor` both report it where it was typed."""
    with pytest.raises(RuntimeUnavailable) as caught:
        agent.build("claude-cli", model="m", nonsense=1, hsot="foundry")
    message = str(caught.value)
    assert "hsot" in message and "nonsense" in message
    assert "runtimes: claude-cli:" in message


# -- argv, as a pure function -----------------------------------------------


def test_stream_json_always_carries_verbose() -> None:
    """Without --verbose the CLI prints an error to stdout and exits 0 — a failure that
    reads as an empty success. Verified against the real binary."""
    argv = build_argv(["claude"])
    assert "--verbose" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_built_in_tools_are_disabled() -> None:
    """`claude -p` defaults to Bash/Edit/Write. An access layer must not ship that."""
    argv = build_argv(["claude"])
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv


@pytest.mark.parametrize(
    ("mode", "web"),
    [("off", ""), ("search", "WebSearch"), ("browse", "WebSearch,WebFetch")],
)
def test_a_web_scope_turns_on_the_web_tools_and_allows_them_by_name(
    mode: str, web: str
) -> None:
    """`--print` has no one to ask, so a tool offered and not allowed is a tool refused
    — the web tools are allowed by name beside ours, and nothing else is turned on."""
    allow = ["mcp__design__wiki_get"]
    argv = build_argv(["claude"], allow=allow, web=claude_cli.WEB_TOOLS[mode])
    assert argv[argv.index("--tools") + 1] == web
    # Last on the line when there is no system prompt, so the rest is the list.
    assert argv[argv.index("--allowedTools") + 1 :] == [
        *allow,
        *filter(None, web.split(",")),
    ]


@pytest.mark.anyio
async def test_each_mode_reaches_the_command_line_from_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    real = claude_cli.build_argv

    def record(command: list[str], **options: object) -> list[str]:
        seen.update(options)
        return real(command, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_cli, "build_argv", record)
    await collect("text", web=WebScope(mode="browse"))
    assert seen["web"] == ("WebSearch", "WebFetch")
    await collect("text")
    assert seen["web"] == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scope",
    [
        WebScope(mode="browse", allowed_domains=["a.example.org"]),
        WebScope(mode="search", blocked_domains=["b.example.org"]),
        WebScope(mode="search", max_uses=2),
    ],
)
async def test_what_the_binary_cannot_hold_to_is_refused_before_it_is_spawned(
    scope: WebScope,
) -> None:
    """The scenario would answer if spawned; the refusal is the only event."""
    events = await collect("text", web=scope)
    assert [type(e).__name__ for e in events] == ["AgentFailed"]
    failed = events[0]
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "web_scope"
    assert failed.remedy == WEB_REMEDY


@pytest.mark.anyio
async def test_a_web_call_is_reported_under_web_as_a_read_with_its_page() -> None:
    """Shaped like the recorded `tool_call` stream rather than recorded itself: the
    binary's web tools carry no server prefix, so neither a declared effect nor a
    source; both are supplied here."""
    events = await collect("web_call", web=WebScope(mode="browse"))
    started = [e for e in events if isinstance(e, ev.ToolStarted)]
    results = [e for e in events if isinstance(e, ev.ToolResult)]
    assert [(e.tool, e.source_id, e.effect) for e in started] == [
        ("WebSearch", "web", "external_read"),
        ("WebFetch", "web", "external_read"),
    ]
    assert results[0].refs == []
    assert results[1].refs == ["web:https://a.example.org/compaction"]
    assert isinstance(events[-1], ev.AgentCompleted)


def test_strict_mcp_config_is_never_conditional() -> None:
    """No servers must mean no tools — never "whatever the user configured globally".

    This used to be gated on `servers`, so a session with nothing servable sent neither
    flag and `claude -p` fell back to the ambient MCP config. Observed in practice: the
    agent reported the operator's unrelated MCP tools as its context and answered from
    them. The empty case is exactly the one that has to be locked shut.
    """
    for argv in (build_argv(["claude"]), build_argv(["claude"], servers={})):
        assert "--strict-mcp-config" in argv
        payload = json.loads(argv[argv.index("--mcp-config") + 1])
        assert payload == {"mcpServers": {}}

    argv = build_argv(["claude"], servers={"design": {"command": "python"}})
    assert "--strict-mcp-config" in argv
    payload = json.loads(argv[argv.index("--mcp-config") + 1])
    assert payload == {"mcpServers": {"design": {"command": "python"}}}


def test_the_model_is_passed_only_when_chosen() -> None:
    assert "--model" not in build_argv(["claude"])
    assert "--model" in build_argv(["claude"], model="sonnet")


@pytest.mark.anyio
async def test_a_persona_and_its_skills_reach_the_shelled_out_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composed prompt goes out as `--append-system-prompt`, so a deployment's voice
    reaches the runtime that owns its own loop as well as the three that do not."""
    from latent_intel.models import Descriptor

    seen: dict[str, object] = {}
    real = claude_cli.build_argv

    def record(command: list[str], **options: object) -> list[str]:
        seen.update(options)
        return real(command, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_cli, "build_argv", record)
    await collect(
        "text",
        sources=[Descriptor(id="design", kind="wiki")],
        persona="You are an archivist.",
        skills=[Skill(name="citation-style", body="Cite inline.")],
    )

    prompt = seen["system_prompt"]
    assert isinstance(prompt, str)
    assert "design (wiki)" in prompt
    assert "You are an archivist." in prompt
    assert "## citation-style" in prompt


# -- the allow-list, from declared effect -----------------------------------


def spec(name: str, effect: Effect) -> ToolSpec:
    return ToolSpec(name=name, source_id="design", description="", effect=effect)


@pytest.mark.parametrize(
    "effect", [Effect.LOCAL_WRITE, Effect.EXTERNAL_WRITE, Effect.DESTRUCTIVE]
)
def test_every_writing_effect_is_withheld_unless_approval_is_auto(
    effect: Effect,
) -> None:
    """There is no interactive approval under --print, so the allow-list is the gate.

    Parametrised over all three writing effects because omitting `DESTRUCTIVE` from the
    check would admit the worst tools under the cautious setting — which is exactly what
    the first version of this did.
    """
    tools = [spec("read", Effect.EXTERNAL_READ), spec("write", effect)]
    servers = {"design": {}}
    assert allowed_tools(tools, servers, "ask") == ["mcp__design__read"]
    assert len(allowed_tools(tools, servers, "auto")) == 2


def test_a_tool_the_agent_cannot_reach_is_never_offered() -> None:
    """A `files` source has no MCP server, so its tools are not in the agent's world."""
    assert allowed_tools([spec("read", Effect.EXTERNAL_READ)], {}, "auto") == []


def test_server_names_are_folded_the_way_claude_folds_them() -> None:
    assert sanitise("claude.ai Hugging Face") == "claude_ai_Hugging_Face"


# -- the recorded streams ---------------------------------------------------


@pytest.mark.anyio
async def test_text_streams_then_completes_once() -> None:
    events = await collect("text")
    tokens = [e for e in events if isinstance(e, ev.AssistantToken)]
    done = [e for e in events if isinstance(e, ev.AgentCompleted)]
    assert [t.text for t in tokens] == ["Compaction ", "is bounded."]
    assert len(done) == 1
    # streamed=True is what stops the renderer printing the answer a second time.
    assert done[0].streamed and done[0].text == "Compaction is bounded."


@pytest.mark.anyio
async def test_usage_keeps_only_the_integers() -> None:
    """claude's usage carries nested dicts and a float cost; ours is dict[str, int]."""
    done = [e for e in await collect("text") if isinstance(e, ev.AgentCompleted)][0]
    assert done.usage == {
        "input_tokens": 12,
        "output_tokens": 5,
        "duration_ms": 900,
        "num_turns": 1,
    }


@pytest.mark.anyio
async def test_a_tool_call_pairs_and_nests() -> None:
    tools = [spec("wiki_get", Effect.EXTERNAL_READ)]
    events = await collect("tool_call", tools=tools, mcp_servers={"design": {}})
    started = [e for e in events if isinstance(e, ev.ToolStarted)][0]
    result = [e for e in events if isinstance(e, ev.ToolResult)][0]
    assert started.tool == "wiki_get" and started.source_id == "design"
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, not guessed
    assert result.ok and result.parent_id == started.event_id


@pytest.mark.anyio
async def test_a_failed_turn_is_a_failure_even_when_subtype_says_success() -> None:
    """The regression test for the sharpest trap: a model-not-found run reports
    `is_error: true` alongside `subtype: "success"`."""
    events = await collect("model_error")
    failed = [e for e in events if isinstance(e, ev.AgentFailed)]
    assert len(failed) == 1
    assert not [e for e in events if isinstance(e, ev.AgentCompleted)]
    assert failed[0].kind == "api_error"


@pytest.mark.anyio
async def test_unparseable_lines_do_not_derail_the_stream() -> None:
    """claude prints non-JSON between JSON objects; a truncated line must not abort."""
    events = await collect("garbage")
    assert [e for e in events if isinstance(e, ev.AssistantToken)]
    assert [e for e in events if isinstance(e, ev.AgentCompleted)]


@pytest.mark.anyio
async def test_silence_with_a_bad_exit_is_reported_rather_than_looking_empty() -> None:
    events = await collect("empty")
    assert len(events) == 1 and isinstance(events[0], ev.AgentFailed)
    assert events[0].kind == "runtime_error"


def test_a_missing_binary_is_unavailable_not_an_exception() -> None:
    assert not ClaudeCliRuntime(command="definitely-not-a-real-binary").available()


@pytest.mark.anyio
async def test_a_missing_binary_is_reported_before_a_scope_it_would_refuse() -> None:
    """The binary is what needs fixing first; a web refusal would send someone to
    change a scope on a runtime that cannot run either way."""
    missing = ClaudeCliRuntime(command="definitely-not-a-real-binary")
    events = [
        event
        async for event in missing.stream(
            [Message(text="q")],
            [],
            emitter=ev.Emitter(uuid4()),
            web=WebScope(mode="search", max_uses=2),
        )
    ]
    assert [(type(e).__name__, getattr(e, "kind", "")) for e in events] == [
        ("AgentFailed", "runtime_error")
    ]


# -- resolving the executable -------------------------------------------------


def _shim(directory: Path, target: str) -> Path:
    """An npm `cmd-shim` as generated on Windows. Only the last line matters: it names
    the program relative to the shim's own directory."""
    shim = directory / "claude.cmd"
    shim.write_text(
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
        f':start\r\nSETLOCAL\r\nCALL :find_dp0\r\n"%dp0%\\{target}" %*\r\n',
        encoding="utf-8",
    )
    return shim


def _which(**table: str | None) -> object:
    return lambda name: table.get(name)


def test_an_npm_shim_to_an_exe_is_unwrapped_to_the_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash on Windows: `which` finds `claude.cmd`, `CreateProcess` given the bare
    name does not. Launching the shim would route through `cmd.exe`, so the resolver
    launches what the shim launches."""
    exe = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    exe.mkdir(parents=True)
    exe = exe / "claude.exe"
    exe.write_bytes(b"")
    shim = _shim(tmp_path, r"node_modules\@anthropic-ai\claude-code\bin\claude.exe")
    monkeypatch.setattr(claude_cli.shutil, "which", _which(claude=str(shim)))
    assert claude_cli.resolve_command(["claude", "-p"]) == [str(exe), "-p"]


def test_an_npm_shim_to_a_script_runs_through_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code"
    script.mkdir(parents=True)
    script = script / "cli.js"
    script.write_text("")
    shim = _shim(tmp_path, r"node_modules\@anthropic-ai\claude-code\cli.js")
    monkeypatch.setattr(
        claude_cli.shutil, "which", _which(claude=str(shim), node="/usr/bin/node")
    )
    assert claude_cli.resolve_command(["claude"]) == ["/usr/bin/node", str(script)]


def test_a_shim_with_no_recognisable_target_is_launched_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo something else entirely\r\n", encoding="utf-8")
    monkeypatch.setattr(claude_cli.shutil, "which", _which(claude=str(shim)))
    assert claude_cli.resolve_command(["claude"]) == [str(shim)]


def test_a_real_binary_passes_through_with_its_arguments() -> None:
    """The fake-interpreter path every subprocess test relies on."""
    assert claude_cli.resolve_command([sys.executable, str(FAKE), "text"]) == [
        sys.executable,
        str(FAKE),
        "text",
    ]


@pytest.mark.anyio
async def test_an_unfindable_command_fails_as_an_event_not_an_exception() -> None:
    """Failures inside `run()` are events: a frontend iterating over a transport has
    nowhere to catch an exception."""
    r = ClaudeCliRuntime(command="definitely-not-a-real-binary")
    events = [
        e async for e in r.stream([Message(text="q")], [], emitter=ev.Emitter(uuid4()))
    ]
    assert len(events) == 1
    assert isinstance(events[0], ev.AgentFailed)
    assert events[0].kind == "runtime_error"


@pytest.mark.anyio
async def test_claude_cli_gets_skill_bodies_and_never_the_engine_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Engine tools are in-process only, so the delegated runtime is given the bodies
    — and `load_skill` must not reach its allow-list, where nothing could serve it."""
    from latent_intel.agent import tools as engine_tools

    seen: dict[str, object] = {}
    real = claude_cli.build_argv

    def record(command: list[str], **options: object) -> list[str]:
        seen.update(options)
        return real(command, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_cli, "build_argv", record)
    skills = [Skill(name="citation-style", body="Cite inline.", description="d")]
    await collect(
        "text",
        tools=engine_tools.specs(skills),
        skills=skills,
        invoked=[Skill(name="deploy", body="Ship it.")],
    )

    prompt = seen["system_prompt"]
    assert isinstance(prompt, str)
    assert "## citation-style\n\nCite inline." in prompt
    assert "## deploy\n\nShip it." in prompt
    assert seen["allow"] == []
