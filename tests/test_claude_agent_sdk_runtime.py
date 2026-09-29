"""The `claude-agent-sdk` runtime: availability, isolation, the turn, and every ending.

No network, no key, no model, no binary. `query` is `fixtures/fake_claude_agent`, which
yields the SDK's own message types and serves a call to one of our tools the way the
SDK does for the binary — through `SdkMcpBridge` into the server this runtime built —
so routing, schema validation and the not-found reply are the SDK's, not a copy.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from uuid import UUID, uuid4

import claude_agent_sdk as sdk
import pytest

from latent_intel import events as ev
from latent_intel.agent import hosts
from latent_intel.agent.base import Delegating
from latent_intel.agent.runtimes.claude_agent_sdk import (
    AGENT_TOOL,
    ENV_HOST,
    HOSTS,
    PREFIX,
    SERVER,
    ClaudeAgentSdkRuntime,
    environment,
)
from latent_intel.models import Effect, Message, RuntimeUnavailable, Skill, ToolSpec
from latent_intel.subagents import Subagent
from tests.fixtures.fake_claude_agent import Call, FakeQuery, Raise, Result, Text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Foundry's two variables, present but meaningless. `conftest` deletes every
    `ANTHROPIC_*` first, so this is the only reason they exist in a test."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "never-printed-key")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "never-printed-resource")


def runtime(fake: FakeQuery | None = None, **options: Any) -> ClaudeAgentSdkRuntime:
    if fake is not None:
        options.setdefault("query_factory", lambda: fake.query)
    return ClaudeAgentSdkRuntime(**options)


def use(backend: ClaudeAgentSdkRuntime, fake: FakeQuery) -> FakeQuery:
    """Point the next turn at `fake`. The runtime is the same object throughout, which
    is what resume depends on."""
    backend.query_factory = lambda: fake.query
    return fake


def spec(name: str, effect: Effect = Effect.EXTERNAL_READ) -> ToolSpec:
    return ToolSpec(
        name=name,
        source_id="design",
        description=f"the {name} tool",
        effect=effect,
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    )


class Router:
    """`Session.call_tool`, recorded. Raises for a tool named `broken`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(
        self, source_id: str, name: str, arguments: dict[str, Any]
    ) -> str:
        self.calls.append((source_id, name, arguments))
        if name == "broken":
            raise RuntimeError("the store is down")
        return f"{name} found {arguments.get('q')}"


async def collect(
    backend: ClaudeAgentSdkRuntime,
    tools: list[ToolSpec] | None = None,
    *,
    messages: list[Message] | None = None,
    session: UUID | None = None,
    **options: Any,
) -> list[ev.AgentEvent]:
    return [
        event
        async for event in backend.stream(
            messages or [Message(text="what is compaction?")],
            tools or [],
            emitter=ev.Emitter(session or uuid4()),
            **options,
        )
    ]


def terminal(events: list[ev.AgentEvent]) -> ev.AgentEvent:
    """Exactly one terminal event per turn — the invariant every case below shares."""
    ends = [e for e in events if isinstance(e, ev.AgentCompleted | ev.AgentFailed)]
    assert len(ends) == 1, [type(e).__name__ for e in events]
    # And in `sequence` order: the relay is drained before the runtime stamps anything
    # of its own, so a stream that runs backwards is a bug, not a race.
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences), sequences
    return ends[0]


# -- availability, offline and by name --------------------------------------


def test_a_runtime_with_nothing_set_names_the_variables_it_wants() -> None:
    """`doctor` builds this with no arguments, which is why it has to be constructible
    with none. The default host is the tool runner's, so switching `runtime:` between
    the two changes only the harness."""
    reason = ClaudeAgentSdkRuntime().unavailable_reason()
    assert reason is not None
    assert "ANTHROPIC_FOUNDRY_API_KEY" in reason
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in reason


def test_values_are_never_printed(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "never-printed-url")
    reason = ClaudeAgentSdkRuntime().unavailable_reason()
    assert reason is not None and "never-printed" not in reason


def test_an_unknown_host_is_named_with_the_ones_there_are() -> None:
    reason = ClaudeAgentSdkRuntime(host="bedrock").unavailable_reason()
    assert reason is not None
    assert (
        "bedrock" in reason and "anthropic" in reason and "foundry-anthropic" in reason
    )


def test_a_missing_sdk_names_the_extra(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "claude_agent_sdk" else real(name, *a),
    )
    reason = ClaudeAgentSdkRuntime().unavailable_reason()
    assert reason is not None and "latent-intel[claude-agent-sdk]" in reason


@pytest.mark.anyio
async def test_a_runtime_that_cannot_run_says_so_as_an_event() -> None:
    fake = FakeQuery(Result())
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"
    assert fake.calls == 0, "nothing is spawned for a turn that cannot run"


def test_a_missing_cli_path_is_reported_before_a_turn(credentials: None) -> None:
    reason = ClaudeAgentSdkRuntime(cli_path="/no/such/claude").unavailable_reason()
    assert reason is not None and "cli_path" in reason


def test_the_host_comes_from_the_option_then_the_variable_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ClaudeAgentSdkRuntime().host == "foundry-anthropic"
    monkeypatch.setenv(ENV_HOST, "anthropic")
    assert ClaudeAgentSdkRuntime().host == "anthropic"
    assert ClaudeAgentSdkRuntime(host="foundry-anthropic").host == "foundry-anthropic"


def test_unknown_and_unhonourable_options_are_refused() -> None:
    with pytest.raises(RuntimeUnavailable) as caught:
        ClaudeAgentSdkRuntime(model="m", max_tokens=5)
    assert "max_tokens" in str(caught.value)
    assert "runtimes: claude-agent-sdk:" in str(caught.value)
    # The SDK drops a zero rather than sending it, which would lift the ceiling.
    with pytest.raises(RuntimeUnavailable):
        ClaudeAgentSdkRuntime(max_tool_rounds=0)
    with pytest.raises(RuntimeUnavailable):
        ClaudeAgentSdkRuntime(max_budget_usd=0)


def test_this_runtime_declares_who_owns_its_loop() -> None:
    assert ClaudeAgentSdkRuntime().family() == "sdk"


def test_every_anthropic_row_is_dialled_or_a_decision_is_owed() -> None:
    """A row added to the table is not reachable here until someone decides how the
    binary is pointed at it — this is where that decision is asked for."""
    assert set(HOSTS) == set(hosts.for_sdk("anthropic"))


# -- isolation --------------------------------------------------------------


@pytest.mark.anyio
async def test_the_binary_is_isolated_from_the_operator_s_own_setup(
    credentials: None,
) -> None:
    """No built-in tools, no settings, no skills listing, strict MCP config, anything
    off the allow-list refused, and prompts delivered verbatim — on every turn."""
    fake = FakeQuery(Result())
    await collect(runtime(fake), [spec("search")])
    options = fake.options
    assert options.tools == []
    assert options.setting_sources == []
    assert options.skills == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.verbatim_prompts is True
    assert options.include_partial_messages is True
    assert options.allowed_tools == [f"{PREFIX}design_search"]
    assert list(options.mcp_servers) == [SERVER]


@pytest.mark.anyio
async def test_a_turn_with_no_tools_serves_no_server(credentials: None) -> None:
    fake = FakeQuery(Result())
    await collect(runtime(fake))
    assert fake.options.mcp_servers == {}
    assert fake.options.allowed_tools == []


@pytest.mark.anyio
async def test_the_prompt_is_the_preset_with_ours_appended(credentials: None) -> None:
    """Claude Code's own prompt, as an Agent SDK application runs it, with our
    inventory, posture and persona after it."""
    fake = FakeQuery(Result())
    await collect(runtime(fake), persona="You are the design desk.")
    prompt = fake.options.system_prompt
    assert prompt["type"] == "preset" and prompt["preset"] == "claude_code"
    assert "You are the design desk." in prompt["append"]


@pytest.mark.anyio
async def test_skills_are_a_listing_naming_load_skill_as_the_binary_offers_it(
    credentials: None,
) -> None:
    """The body loads when the model asks, through the engine tool served like any
    other — so the listing has to name it with the binary's prefix, or it sends the
    model after a tool it does not have. A skill run with `/name` is sent whole."""
    from latent_intel.agent import tools as engine_tools

    loader = engine_tools.specs(
        [Skill(name="citation-style", body="x", description="d")]
    )
    fake = FakeQuery(Result())
    await collect(
        runtime(fake),
        loader,
        skills=[
            Skill(
                name="citation-style", body="Cite inline.", description="How to cite."
            )
        ],
        invoked=[Skill(name="deploy", body="Ship it.")],
    )
    prompt = fake.options.system_prompt["append"]
    assert "- citation-style — How to cite." in prompt
    assert f"`{PREFIX}engine_load_skill`" in prompt
    assert f"{PREFIX}engine_load_skill" in fake.options.allowed_tools
    assert "Cite inline." not in prompt
    assert "## deploy\n\nShip it." in prompt


@pytest.mark.anyio
async def test_limits_and_model_reach_the_binary(credentials: None) -> None:
    fake = FakeQuery(Result())
    await collect(
        runtime(fake, model="claude-haiku-4-5", max_tool_rounds=4, max_budget_usd=0.5)
    )
    assert fake.options.model == "claude-haiku-4-5"
    assert fake.options.max_turns == 4
    assert fake.options.max_budget_usd == 0.5
    assert fake.prompt == "what is compaction?"


def test_only_the_chosen_row_reaches_the_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child inherits our environment, so anything that could send it elsewhere is
    blanked rather than left out: the other row's variables, the other providers'
    switches, and the tokens the binary would prefer over the row's key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-key")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "foundry-key")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "foundry-resource")

    direct = environment("anthropic")
    assert direct["ANTHROPIC_API_KEY"] == "direct-key"
    assert direct["ANTHROPIC_FOUNDRY_API_KEY"] == ""
    assert direct["ANTHROPIC_FOUNDRY_RESOURCE"] == ""
    assert direct["CLAUDE_CODE_USE_FOUNDRY"] == ""

    foundry = environment("foundry-anthropic")
    assert foundry["CLAUDE_CODE_USE_FOUNDRY"] == "1"
    assert foundry["ANTHROPIC_FOUNDRY_API_KEY"] == "foundry-key"
    assert foundry["ANTHROPIC_API_KEY"] == ""

    for env in (direct, foundry):
        # A subagent's own `model:` must win over an export on the operator's shell;
        # the model aliases a Foundry deployment names its deployments with pass.
        assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == ""
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
        assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
        assert env["CLAUDE_CODE_USE_VERTEX"] == ""
        assert env["ANTHROPIC_AUTH_TOKEN"] == ""
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


@pytest.mark.anyio
async def test_writing_tools_are_withheld_unless_approval_is_auto(
    credentials: None,
) -> None:
    tools = [spec("search"), spec("delete", Effect.DESTRUCTIVE)]
    cautious = FakeQuery(Result())
    await collect(runtime(cautious), tools)
    assert cautious.options.allowed_tools == [f"{PREFIX}design_search"]

    permissive = FakeQuery(Result())
    await collect(runtime(permissive, approval="auto"), tools)
    assert permissive.options.allowed_tools == [
        f"{PREFIX}design_search",
        f"{PREFIX}design_delete",
    ]


# -- the ordinary turn ------------------------------------------------------


@pytest.mark.anyio
async def test_text_streams_then_completes_once_with_usage_and_cost(
    credentials: None,
) -> None:
    fake = FakeQuery(Text("Compaction "), Text("is bounded."), Result(result="ignored"))
    events = await collect(runtime(fake))

    assert [e.text for e in events if isinstance(e, ev.AssistantToken)] == [
        "Compaction ",
        "is bounded.",
    ]
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.streamed and done.text == "Compaction is bounded."
    # Integers only: the nested `server_tool_use` the binary reports is not carried.
    assert done.usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 3,
        "duration_ms": 42,
        "num_turns": 1,
    }
    assert done.cost_usd == 0.0123
    assert fake.closed, "the SDK's generator is closed on the way out"


@pytest.mark.anyio
async def test_an_answer_that_did_not_stream_is_the_result(credentials: None) -> None:
    fake = FakeQuery(Result(result="Assembled answer."))
    done = terminal(await collect(runtime(fake)))
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "Assembled answer." and not done.streamed


@pytest.mark.anyio
async def test_a_subagent_s_words_are_not_the_answer(credentials: None) -> None:
    """A subagent's stream carries its parent's tool-use id; its text is its report to
    that agent, and streaming it would splice it into the answer."""
    fake = FakeQuery(
        Text("scratch notes", parent="toolu_agent"), Text("The answer."), Result()
    )
    done = terminal(await collect(runtime(fake)))
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "The answer."


# -- our tools, served in-process --------------------------------------------


@pytest.mark.anyio
async def test_a_tool_is_routed_once_and_its_pair_lands_before_the_next_text(
    credentials: None,
) -> None:
    router = Router()
    fake = FakeQuery(
        Text("Let me check."),
        Call(f"{PREFIX}design_search", {"q": "compaction"}),
        Text("Found it."),
        Result(),
    )
    events = await collect(runtime(fake), [spec("search")], call_tool=router)

    assert router.calls == [("design", "search", {"q": "compaction"})]
    kinds = [type(e).__name__ for e in events]
    assert kinds.index("ToolStarted") < kinds.index("ToolResult")
    tokens = [i for i, e in enumerate(events) if isinstance(e, ev.AssistantToken)]
    result_at = kinds.index("ToolResult")
    assert tokens[0] < result_at < tokens[-1]
    started = next(e for e in events if isinstance(e, ev.ToolStarted))
    assert started.tool == "search" and started.source_id == "design"
    assert started.effect == str(Effect.EXTERNAL_READ)
    # The model was shown the router's own output.
    assert fake.served["toolu_1"]["content"][0]["text"] == "search found compaction"
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "Let me check.\n\nFound it."


@pytest.mark.anyio
async def test_a_failing_tool_is_an_error_the_model_sees(credentials: None) -> None:
    router = Router()
    fake = FakeQuery(Call(f"{PREFIX}design_broken", {"q": "x"}), Result())
    events = await collect(runtime(fake), [spec("broken")], call_tool=router)

    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert not result.ok and "the store is down" in result.error
    assert fake.served["toolu_1"]["isError"] is True
    assert "the store is down" in fake.served["toolu_1"]["content"][0]["text"]
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_arguments_that_fail_the_schema_never_reach_the_router(
    credentials: None,
) -> None:
    """A difference from the custom loop, on purpose: the SDK validates arguments
    against the tool's schema before calling us, so no pair is emitted. The model is
    still told."""
    router = Router()
    fake = FakeQuery(Call(f"{PREFIX}design_search", {}), Result())
    events = await collect(runtime(fake), [spec("search")], call_tool=router)

    assert router.calls == []
    assert not any(isinstance(e, ev.ToolStarted) for e in events)
    assert fake.served["toolu_1"]["isError"] is True


@pytest.mark.anyio
async def test_two_tools_cut_to_one_served_name_are_refused(credentials: None) -> None:
    """The binary's prefix takes room from the name, and two names that only differ
    past the cut would otherwise route one tool's calls to the other."""
    long = "x" * 60
    tools = [
        ToolSpec(
            name=f"{long}a", source_id="s", description="", effect="external_read"
        ),
        ToolSpec(
            name=f"{long}b", source_id="s", description="", effect="external_read"
        ),
    ]
    fake = FakeQuery(Result())
    failed = terminal(await collect(runtime(fake), tools))
    assert isinstance(failed, ev.AgentFailed)
    assert "s.xxx" in failed.message and fake.calls == 0


# -- the binary's own web tools, opted into ---------------------------------


def test_only_the_web_reading_tools_can_be_opted_into() -> None:
    """Local files belong to our `files` connector, with declared effects; nothing
    that writes is offered at all."""
    with pytest.raises(RuntimeUnavailable) as caught:
        ClaudeAgentSdkRuntime(builtin_tools=["WebSearch", "Bash"])
    assert "Bash" in str(caught.value)
    assert "WebSearch" in str(caught.value) and "WebFetch" in str(caught.value)


@pytest.mark.anyio
async def test_an_opted_in_tool_is_offered_and_allowed(credentials: None) -> None:
    fake = FakeQuery(Result())
    await collect(runtime(fake, builtin_tools=["WebSearch"]), [spec("search")])
    assert fake.options.tools == ["WebSearch"]
    assert fake.options.allowed_tools == [f"{PREFIX}design_search", "WebSearch"]


@pytest.mark.anyio
async def test_a_web_tool_call_is_a_pair_built_from_the_binary_s_messages(
    credentials: None,
) -> None:
    fake = FakeQuery(
        Call(
            "WebSearch", {"query": "compaction"}, id="toolu_w", output="three results"
        ),
        Call("WebFetch", {"url": "u"}, id="toolu_f", output="refused", is_error=True),
        Text("Done."),
        Result(),
    )
    events = await collect(runtime(fake, builtin_tools=["WebSearch", "WebFetch"]))

    starts = [e for e in events if isinstance(e, ev.ToolStarted)]
    results = [e for e in events if isinstance(e, ev.ToolResult)]
    assert [s.tool for s in starts] == ["WebSearch", "WebFetch"]
    assert {s.source_id for s in starts} == {"claude-agent-sdk"}
    assert {s.effect for s in starts} == {str(Effect.EXTERNAL_READ)}
    assert starts[0].arguments == {"query": "compaction"}
    assert results[0].ok and results[0].output == "three results"
    assert not results[1].ok and results[1].error == "refused"
    assert [r.parent_id for r in results] == [s.event_id for s in starts]


@pytest.mark.anyio
async def test_a_subagent_s_web_call_is_reported_too(credentials: None) -> None:
    fake = FakeQuery(
        Call("WebSearch", {"query": "q"}, id="toolu_s", parent="toolu_agent"),
        Text("Answer."),
        Result(),
    )
    events = await collect(runtime(fake, builtin_tools=["WebSearch"]))
    assert [type(e).__name__ for e in events if isinstance(e, ev.ToolStarted)] == [
        "ToolStarted"
    ]
    assert any(isinstance(e, ev.ToolResult) for e in events)
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted) and done.text == "Answer."


# -- subagents --------------------------------------------------------------


def delegate(name: str, tools: tuple[str, ...] | None) -> Subagent:
    return Subagent(
        name=name, description=f"the {name}", prompt=f"You {name}.", tools=tools
    )


@pytest.mark.anyio
async def test_subagents_are_defined_with_their_tools_renamed(
    credentials: None,
) -> None:
    """Named as `/tools` prints them, served as the binary calls them. A name that
    resolves to nothing is dropped; an agent left with none gets an empty list, never
    `None`, which would hand it every tool the main agent has."""
    fake = FakeQuery(Result())
    await collect(
        runtime(fake, builtin_tools=["WebSearch"]),
        [spec("search")],
        agents=[
            delegate("reviewer", ("design.search", "WebSearch", "nope.tool")),
            delegate("generalist", None),
            delegate("stranded", ("nope.tool",)),
        ],
    )
    defined = fake.options.agents
    assert defined["reviewer"].tools == [f"{PREFIX}design_search", "WebSearch"]
    assert defined["reviewer"].prompt == "You reviewer."
    assert defined["reviewer"].description == "the reviewer"
    assert defined["generalist"].tools is None
    assert defined["stranded"].tools == []
    assert defined["reviewer"].disallowedTools is None
    assert AGENT_TOOL in fake.options.tools
    # Each project agent by name: the bare tool would admit Claude Code's own.
    assert AGENT_TOOL not in fake.options.allowed_tools
    assert {f"{AGENT_TOOL}({name})" for name in defined} <= set(
        fake.options.allowed_tools
    )


@pytest.mark.anyio
async def test_disallowed_tools_are_renamed_like_tools(credentials: None) -> None:
    """Honoured rather than reported and dropped: ignoring it would hand the delegate
    tools its file refuses. A name that is not ours passes through as Claude Code's."""
    fake = FakeQuery(Result())
    careful = Subagent(
        name="careful",
        description="d",
        prompt="p",
        disallowed=("design.search", "WebFetch"),
    )
    await collect(runtime(fake), [spec("search")], agents=[careful])
    defined = fake.options.agents["careful"]
    assert defined.tools is None
    assert defined.disallowedTools == [f"{PREFIX}design_search", "WebFetch"]


@pytest.mark.anyio
async def test_no_subagents_means_no_agent_tool(credentials: None) -> None:
    fake = FakeQuery(Result())
    await collect(runtime(fake))
    assert fake.options.agents is None
    assert AGENT_TOOL not in fake.options.tools


@pytest.mark.anyio
async def test_a_hand_off_is_a_pair_whose_result_is_the_report(
    credentials: None,
) -> None:
    fake = FakeQuery(
        Call(
            AGENT_TOOL,
            {"subagent_type": "reviewer", "prompt": "review this draft"},
            id="toolu_agent",
            output="Three claims checked.",
        ),
        Text("Summary."),
        Result(),
    )
    events = await collect(runtime(fake), agents=[delegate("reviewer", None)])

    started = next(e for e in events if isinstance(e, ev.ToolStarted))
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert started.tool == AGENT_TOOL and started.effect == str(Effect.NONE)
    assert result.ok and result.output == "Three claims checked."
    assert result.parent_id == started.event_id


def test_this_runtime_declares_it_runs_subagents_and_with_what() -> None:
    """Delegating is declared by implementing it; what a subagent may name passes the
    same effect gate the main agent's tools do."""
    tools = [spec("search"), spec("delete", Effect.DESTRUCTIVE)]
    cautious = ClaudeAgentSdkRuntime(builtin_tools=["WebFetch"])
    assert isinstance(cautious, Delegating)
    assert cautious.subagent_tools(tools) == {"design.search", "WebFetch"}
    permissive = ClaudeAgentSdkRuntime(approval="auto")
    assert permissive.subagent_tools(tools) == {"design.search", "design.delete"}


def test_the_tool_runner_does_not_run_subagents() -> None:
    from latent_intel.agent.runtimes.anthropic_sdk import AnthropicSdkRuntime

    assert not isinstance(AnthropicSdkRuntime(), Delegating)


@pytest.mark.anyio
async def test_a_failure_mid_turn_reports_what_ran_and_closes_what_did_not(
    credentials: None,
) -> None:
    """Our tool ran and the binary died before saying so; a web search started and
    never answered. The first is reported — a write that happened must be seen — and
    the second is closed, so no renderer waits on it. Then the one failure."""
    router = Router()
    fake = FakeQuery(
        Call("WebSearch", {"query": "q"}, id="toolu_w", shown="asked"),
        Call(f"{PREFIX}design_search", {"q": "x"}, shown="none"),
        Raise(sdk.ProcessError("exited", exit_code=1)),
    )
    events = await collect(
        runtime(fake, builtin_tools=["WebSearch"]), [spec("search")], call_tool=router
    )

    assert router.calls == [("design", "search", {"q": "x"})]
    pairs = [
        (type(e).__name__, getattr(e, "tool", ""), getattr(e, "ok", None))
        for e in events
        if isinstance(e, ev.ToolStarted | ev.ToolResult)
    ]
    assert pairs == [
        ("ToolStarted", "WebSearch", None),
        ("ToolStarted", "search", None),
        ("ToolResult", "search", True),
        ("ToolResult", "WebSearch", False),
    ]
    assert isinstance(terminal(events), ev.AgentFailed)


# -- every way a turn can end -----------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "kind"),
    [
        (Result(is_error=True, subtype="error_max_turns", num_turns=4), "tool_rounds"),
        (
            Result(is_error=True, subtype="error_max_budget_usd", num_turns=2),
            "budget",
        ),
        (Result(is_error=True, api_error_status=401, num_turns=0), "auth"),
        (Result(is_error=True, api_error_status=404, num_turns=0), "model_not_found"),
        (Result(is_error=True, api_error_status=429, num_turns=0), "rate_limit"),
        (Result(is_error=True, api_error_status=500, result="overloaded"), "api_error"),
        (
            Result(is_error=True, subtype="error_during_execution", errors=["boom"]),
            "error_during_execution",
        ),
    ],
)
async def test_an_error_result_is_one_failure_of_its_own_kind(
    credentials: None, result: Result, kind: str
) -> None:
    failed = terminal(await collect(runtime(FakeQuery(result), max_budget_usd=0.25)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == kind


@pytest.mark.anyio
async def test_is_error_is_the_truth_not_subtype(credentials: None) -> None:
    """The binary reports a failed request as `subtype: success` with `is_error` set —
    the trap `claude_cli.py` records."""
    fake = FakeQuery(Result(is_error=True, subtype="success", api_error_status=404))
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "model_not_found"


@pytest.mark.anyio
async def test_the_error_raised_after_an_error_result_is_not_a_second_failure(
    credentials: None,
) -> None:
    """The binary exits non-zero after an error result, and the SDK raises for it after
    yielding the result — one turn, one terminal event."""
    fake = FakeQuery(
        Result(is_error=True, subtype="error_max_turns", num_turns=3),
        Raise(sdk.ProcessError("exited", exit_code=1)),
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "tool_rounds"


@pytest.mark.anyio
async def test_a_stream_with_no_result_is_not_a_success(credentials: None) -> None:
    fake = FakeQuery(Text("half an ans"))
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "unexpected_stop"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (sdk.CLINotFoundError("Claude Code not found"), "runtime_unavailable"),
        (sdk.CLIConnectionError("Working directory does not exist"), "connection"),
        (sdk.ProcessError("exited", exit_code=2), "runtime_error"),
        (RuntimeError("anything else"), "runtime_error"),
    ],
)
async def test_an_sdk_error_before_a_result_is_one_failure(
    credentials: None, error: BaseException, kind: str
) -> None:
    fake = FakeQuery(Text("partial"), Raise(error))
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == kind


@pytest.mark.anyio
async def test_closing_early_closes_the_sdk_in_this_task(credentials: None) -> None:
    """The consumer stops after the first token — Ctrl-C in the shell. `async for` does
    not close what it iterates when its own generator is closed, so without `aclosing`
    at both levels the SDK's generator would be left for the garbage collector to
    finalize in some other task, with the binary still running until then."""
    fake = FakeQuery(Text("one"), Text("two"), Result())
    stream = runtime(fake).stream([Message(text="q")], [], emitter=ev.Emitter(uuid4()))
    first = await stream.__anext__()
    assert isinstance(first, ev.AssistantToken)
    await stream.aclose()
    assert fake.closed


# -- resume -----------------------------------------------------------------


def turn_one() -> list[Message]:
    return [Message(text="first question")]


def turn_two(answer: str = "First answer.") -> list[Message]:
    return [
        Message(text="first question"),
        Message(role="assistant", text=answer),
        Message(text="second question"),
    ]


@pytest.mark.anyio
async def test_a_follow_up_resumes_the_binary_s_session(credentials: None) -> None:
    backend = runtime(
        fake := FakeQuery(Text("First answer."), Result(session_id="abc"))
    )
    session = uuid4()
    await collect(backend, messages=turn_one(), session=session)
    assert fake.options.resume is None

    second = use(backend, FakeQuery(Result()))
    await collect(backend, messages=turn_two(), session=session)
    assert second.options.resume == "abc"
    assert second.prompt == "second question", "only the new question is sent"


@pytest.mark.anyio
async def test_a_history_that_does_not_continue_starts_fresh(credentials: None) -> None:
    backend = runtime(FakeQuery(Text("First answer."), Result(session_id="abc")))
    session = uuid4()
    await collect(backend, messages=turn_one(), session=session)

    second = use(backend, FakeQuery(Result()))
    await collect(backend, messages=turn_two("A different answer."), session=session)
    assert second.options.resume is None


@pytest.mark.anyio
async def test_another_session_never_resumes_this_one(credentials: None) -> None:
    backend = runtime(FakeQuery(Text("First answer."), Result(session_id="abc")))
    await collect(backend, messages=turn_one(), session=uuid4())

    second = use(backend, FakeQuery(Result()))
    await collect(backend, messages=turn_two(), session=uuid4())
    assert second.options.resume is None


@pytest.mark.anyio
async def test_a_turn_that_ran_and_failed_keeps_the_thread(credentials: None) -> None:
    """The session appends a question without an answer when a turn fails; the next
    question still continues the binary's session, which holds that attempt."""
    backend = runtime(FakeQuery(Text("First answer."), Result(session_id="abc")))
    session = uuid4()
    await collect(backend, messages=turn_one(), session=session)

    use(
        backend,
        FakeQuery(Result(is_error=True, subtype="error_max_turns", num_turns=3)),
    )
    await collect(backend, messages=turn_two(), session=session)

    third = use(backend, FakeQuery(Result()))
    await collect(
        backend,
        messages=[*turn_two(), Message(text="third question")],
        session=session,
    )
    assert third.options.resume == "abc"


async def _third_turn_resumes(backend: ClaudeAgentSdkRuntime, session: UUID) -> Any:
    third = use(backend, FakeQuery(Result()))
    await collect(
        backend,
        messages=[*turn_two(), Message(text="third question")],
        session=session,
    )
    return third.options.resume


@pytest.mark.anyio
async def test_a_refused_resume_drops_the_thread(credentials: None) -> None:
    """Nothing ran and no endpoint answered: the resume itself failed, and resuming
    that session again would fail the same way."""
    backend = runtime(FakeQuery(Text("First answer."), Result(session_id="abc")))
    session = uuid4()
    await collect(backend, messages=turn_one(), session=session)

    use(
        backend,
        FakeQuery(Result(is_error=True, num_turns=0, errors=["No conversation found"])),
    )
    await collect(backend, messages=turn_two(), session=session)
    assert await _third_turn_resumes(backend, session) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "second",
    [
        FakeQuery(Result(is_error=True, api_error_status=429, num_turns=0)),
        FakeQuery(Raise(sdk.CLIConnectionError("could not start"))),
    ],
    ids=["rate-limited", "never-started"],
)
async def test_a_transient_failure_keeps_the_conversation(
    credentials: None, second: FakeQuery
) -> None:
    """A rate limit, a 5xx or a binary that never started runs nothing too — but the
    session is still there, and dropping it would forget every earlier turn."""
    backend = runtime(FakeQuery(Text("First answer."), Result(session_id="abc")))
    session = uuid4()
    await collect(backend, messages=turn_one(), session=session)

    use(backend, second)
    failed = terminal(await collect(backend, messages=turn_two(), session=session))
    assert isinstance(failed, ev.AgentFailed)
    assert await _third_turn_resumes(backend, session) == "abc"
