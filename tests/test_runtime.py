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
from latent_intel.agent.runtimes import claude_cli
from latent_intel.agent.runtimes.claude_cli import (
    ClaudeCliRuntime,
    allowed_tools,
    build_argv,
    sanitise,
)
from latent_intel.models import Effect, Message, RuntimeUnavailable, ToolSpec

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
    """Absent and unusable are different states. Both import their SDK inside a turn,
    so each is listed — with a reason — on a machine with no credentials, rather than
    vanishing and taking the diagnosis with it."""
    assert "anthropic" in agent.available_kinds()
    assert "openai" in agent.available_kinds()


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
