"""The `openai-agents` runtime: availability, the turn, and every way a turn can end.

`test_openai_runtime.py` case for case, because the promise of this runtime is that it
answers as its sibling does with a framework driving the loop — a case that passes there
and not here is a difference no one asked for, and the three that differ on purpose are
named in the module docstring and asserted below rather than left to be discovered.

No network, no key, no model. The runner is `fixtures/fake_agents`, which stands in for
`agents.Runner` and nothing below it: the model, the client and the transport are never
reached, and the tools are called by the fake runner exactly where the real one calls
them.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from uuid import uuid4

import agents
import openai
import pytest

from latent_intel import events as ev
from latent_intel.agent.runtimes.openai_agents import ENV_HOST, OpenAIAgentsRuntime
from latent_intel.models import Effect, Message, RuntimeUnavailable, ToolSpec
from tests.fixtures.fake_agents import (
    FakeRunner,
    InputTokensDetails,
    Round,
    Usage,
)

try:  # the installed SDK (3.x) builds its errors from httpx2; older lines used httpx
    import httpx2 as httpx
except ModuleNotFoundError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Foundry's two variables, present but meaningless. `conftest` deletes every
    `FOUNDRY_*` first, so this is the only reason they exist in a test."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "never-printed-key")
    monkeypatch.setenv("FOUNDRY_RESOURCE", "never-printed-resource")


class FakeClient:
    """The async context manager `_client()` returns. It is never asked for anything:
    the model object holds it and the model is never called, because the runner is the
    fake."""

    def __init__(self) -> None:
        self.closed = False

    def __call__(self, **_: Any) -> FakeClient:
        return self

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        self.closed = True
        return False


def runtime(fake: FakeRunner | None = None, **options: Any) -> OpenAIAgentsRuntime:
    """The host this branch was written for, plus the model it has no default for."""
    options.setdefault("host", "foundry-openai")
    options.setdefault("model", "gpt-5-deployment")
    options.setdefault("client_factory", FakeClient())
    if fake is not None:
        options.setdefault("runner_factory", fake)
    return OpenAIAgentsRuntime(**options)


def spec(name: str, effect: Effect = Effect.EXTERNAL_READ) -> ToolSpec:
    return ToolSpec(
        name=name, source_id="design", description=f"the {name} tool", effect=effect
    )


async def collect(
    backend: OpenAIAgentsRuntime,
    tools: list[ToolSpec] | None = None,
    **options: Any,
) -> list[ev.AgentEvent]:
    return [
        event
        async for event in backend.stream(
            [Message(text="what is compaction?")],
            tools or [],
            emitter=ev.Emitter(uuid4()),
            **options,
        )
    ]


def _response(status: int) -> Any:
    return httpx.Response(status, request=httpx.Request("POST", "https://example"))


def terminal(events: list[ev.AgentEvent]) -> ev.AgentEvent:
    """Exactly one terminal event per turn — the invariant every case below shares."""
    ends = [e for e in events if isinstance(e, ev.AgentCompleted | ev.AgentFailed)]
    assert len(ends) == 1, [type(e).__name__ for e in events]
    return ends[0]


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    return f"{source_id}/{name} says yes"


# -- availability, offline and by name --------------------------------------


def test_a_runtime_with_nothing_set_names_the_variable_it_wants() -> None:
    """`doctor` builds this with no arguments, which is why it has to be constructible
    with none."""
    reason = OpenAIAgentsRuntime().unavailable_reason()
    assert reason is not None and "OPENAI_API_KEY" in reason


def test_the_foundry_host_names_both_its_variables_and_their_stand_ins() -> None:
    """The same rows as `custom` reaches on this protocol, read from one table rather
    than declared twice — so a row that grew a fallback grew it for both runtimes."""
    reason = OpenAIAgentsRuntime(host="foundry-openai").unavailable_reason()
    assert reason is not None
    assert "FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY)" in reason
    assert "FOUNDRY_RESOURCE (or ANTHROPIC_FOUNDRY_RESOURCE)" in reason


def test_an_unknown_host_names_the_known_ones() -> None:
    reason = OpenAIAgentsRuntime(host="bedrock").unavailable_reason()
    assert reason is not None
    assert "bedrock" in reason and "openrouter" in reason and "local" in reason


def test_no_credential_value_ever_reaches_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor` output is pasted into support threads. Names only, always."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "sk-sentinel-value")
    reason = OpenAIAgentsRuntime(host="foundry-openai").unavailable_reason()
    assert reason is not None
    assert "sk-sentinel-value" not in reason


def test_a_configured_runtime_with_no_model_says_so_under_its_own_key(
    credentials: None,
) -> None:
    """Every host names its models differently, so the block the reason names has to be
    this runtime's, not the sibling's."""
    reason = OpenAIAgentsRuntime(host="foundry-openai").unavailable_reason()
    assert reason is not None
    assert "no model is set" in reason
    assert "runtimes: openai-agents:" in reason


def test_host_selection_reads_this_runtime_s_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its own variable, not the sibling's: a machine comparing the two loops sets one
    against OpenRouter and the other against the public API."""
    assert OpenAIAgentsRuntime().host == "openai"
    monkeypatch.setenv("LATENT_INTEL_OPENAI_HOST", "openrouter")
    assert OpenAIAgentsRuntime().host == "openai"
    monkeypatch.setenv(ENV_HOST, "openrouter")
    assert OpenAIAgentsRuntime().host == "openrouter"
    assert OpenAIAgentsRuntime(host="local").host == "local"


def test_a_config_key_this_runtime_does_not_know_is_rejected_by_name() -> None:
    """The keys are named because the remedy is to fix or delete them, and the block is
    named because a generic refusal says which file but not which line."""
    with pytest.raises(RuntimeUnavailable) as caught:
        OpenAIAgentsRuntime(model="m", nonsense=1, cwd="/tmp")
    message = str(caught.value)
    assert "cwd" in message and "nonsense" in message
    assert "runtimes: openai-agents:" in message


@pytest.mark.anyio
async def test_a_base_install_gets_an_event_naming_the_extra_not_an_ImportError(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    """The guarantee for an install without `[agents]`: `find_spec` answers first, so
    the framework is never reached. `openai` is present in this case, which is what
    makes the reason name the right one of the two extras."""
    import builtins

    real_import = builtins.__import__
    real_find_spec = importlib.util.find_spec

    def no_spec(name: str, package: str | None = None) -> Any:
        return None if name == "agents" else real_find_spec(name, package)

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("agents"):
            raise AssertionError("the openai-agents runtime reached for the SDK anyway")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", no_spec)
    monkeypatch.setattr(builtins, "__import__", refuse)

    failed = terminal(await collect(runtime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"
    assert "latent-intel[agents]" in failed.message


@pytest.mark.anyio
async def test_a_missing_client_sdk_names_the_other_extra(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    """Two packages, two extras: a reason naming `[agents]` when `openai` is what is
    missing sends someone to install the thing they already have."""
    real_find_spec = importlib.util.find_spec

    def no_spec(name: str, package: str | None = None) -> Any:
        return None if name == "openai" else real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", no_spec)
    failed = terminal(await collect(runtime()))
    assert isinstance(failed, ev.AgentFailed)
    assert "latent-intel[api]" in failed.message


@pytest.mark.anyio
async def test_an_unusable_runtime_fails_as_an_event_not_an_exception() -> None:
    """A frontend iterating this over a transport has nowhere to catch an exception."""
    failed = terminal(await collect(OpenAIAgentsRuntime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"


def test_this_runtime_declares_who_owns_its_loop() -> None:
    """Declared, never inferred — `doctor` groups on this."""
    assert OpenAIAgentsRuntime().family() == "sdk"


# -- the ordinary turn ------------------------------------------------------


@pytest.mark.anyio
async def test_text_streams_then_completes_once(credentials: None) -> None:
    client = FakeClient()
    fake = FakeRunner(Round(text=("Compaction ", "is bounded.")))
    events = await collect(runtime(fake, client_factory=client))

    assert [e.text for e in events if isinstance(e, ev.AssistantToken)] == [
        "Compaction ",
        "is bounded.",
    ]
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    # streamed=True is what stops the renderer printing the answer a second time.
    assert done.streamed and done.text == "Compaction is bounded."
    assert client.closed, "the client is an async context manager and must be closed"


@pytest.mark.anyio
async def test_tracing_is_disabled_on_every_run(credentials: None) -> None:
    """The SDK exports traces to OpenAI with `OPENAI_API_KEY` by default — a client's
    transcript leaving for a third party, and a paid call nobody asked for."""
    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake))
    assert fake.runs[0]["run_config"].tracing_disabled is True


@pytest.mark.anyio
async def test_the_ceiling_is_the_runner_s(credentials: None) -> None:
    """`max_turns=N` permits N model calls — the SDK increments the turn and then
    compares with `>` — so it is the same bound `max_tool_rounds` is."""
    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake, max_tool_rounds=4))
    assert fake.runs[0]["max_turns"] == 4


@pytest.mark.anyio
async def test_a_turn_with_no_tools_gives_the_agent_none(credentials: None) -> None:
    """With no sources there are no instructions either: an empty system prompt is
    dropped rather than sent as an empty string."""
    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake))
    agent = fake.runs[0]["agent"]
    assert agent.tools == []
    assert agent.instructions is None
    assert [m["role"] for m in fake.runs[0]["input"]] == ["user"]


@pytest.mark.anyio
async def test_attached_sources_reach_the_model_as_instructions(
    credentials: None,
) -> None:
    """The system prompt is a parameter on this SDK rather than a first message."""
    from latent_intel.models import Descriptor

    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake), sources=[Descriptor(id="design", kind="wiki")])
    assert "design (wiki)" in fake.runs[0]["agent"].instructions


@pytest.mark.anyio
async def test_the_tools_reach_the_agent_as_non_strict_function_tools(
    credentials: None,
) -> None:
    """Strict mode has to be off: a connector's schema is whatever the source declared,
    and strict rejects most of them outright."""
    fake = FakeRunner(Round(text=("hi",)))
    await collect(
        runtime(fake), tools=[spec("wiki_get"), spec("wiki_search")], call_tool=_router
    )
    tools = fake.runs[0]["agent"].tools
    assert [t.name for t in tools] == ["design_wiki_get", "design_wiki_search"]
    assert all(isinstance(t, agents.FunctionTool) for t in tools)
    assert not any(t.strict_json_schema for t in tools)


@pytest.mark.anyio
async def test_a_tool_that_declares_no_parameters_is_still_a_legal_definition(
    credentials: None,
) -> None:
    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake), tools=[spec("ping")])
    assert fake.runs[0]["agent"].tools[0].params_json_schema == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.anyio
async def test_the_token_limit_goes_out_under_the_row_s_own_parameter(
    credentials: None,
) -> None:
    """The SDK hardcodes `max_tokens`, which reasoning deployments reject — so a row
    naming `max_completion_tokens` sends it through `extra_args` with `max_tokens`
    unset, and nothing goes out under both names."""
    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake, max_tokens=4096))
    settings = fake.runs[0]["agent"].model_settings
    assert settings.max_tokens is None
    assert settings.extra_args == {"max_completion_tokens": 4096}


@pytest.mark.anyio
async def test_a_row_that_wants_the_older_name_gets_the_parameter_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter normalises `max_tokens` per vendor, which is what it documents — the
    `tokens_param` precedent, honoured rather than dropped by the framework."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    fake = FakeRunner(Round(text=("hi",)))
    await collect(
        runtime(
            fake, host="openrouter", model="anthropic/claude-sonnet-5", max_tokens=2048
        )
    )
    settings = fake.runs[0]["agent"].model_settings
    assert settings.max_tokens == 2048
    assert settings.extra_args is None


@pytest.mark.anyio
async def test_usage_is_asked_for_rather_than_left_to_the_default(
    credentials: None,
) -> None:
    """The SDK only sends `stream_options` unasked when the client points at
    `api.openai.com`, so every other row would report nothing about what a turn cost."""
    fake = FakeRunner(Round(text=("hi",)))
    await collect(runtime(fake))
    assert fake.runs[0]["agent"].model_settings.include_usage is True


@pytest.mark.anyio
async def test_usage_is_summed_across_rounds_not_taken_from_the_last(
    credentials: None,
) -> None:
    """A tool-heavy question is several round-trips; the last one's usage is a small
    fraction of what it cost."""
    fake = FakeRunner(
        Round(
            tool_calls=(("call_1", "design_wiki_get", '{"key": "gist"}'),),
            usage=Usage(
                input_tokens=100,
                output_tokens=20,
                input_tokens_details=InputTokensDetails(cached_tokens=64),
            ),
        ),
        Round(text=("done",), usage=Usage(input_tokens=200, output_tokens=5)),
    )
    done = terminal(
        await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=_router)
    )
    assert isinstance(done, ev.AgentCompleted)
    assert done.usage["input_tokens"] == 300
    assert done.usage["output_tokens"] == 25
    assert done.usage["cache_read_input_tokens"] == 64
    assert done.usage["num_turns"] == 2
    assert "duration_ms" in done.usage


@pytest.mark.anyio
async def test_a_round_the_host_reported_no_usage_for_is_still_a_round(
    credentials: None,
) -> None:
    """`num_turns` is the number of round-trips, not the number that answered."""
    fake = FakeRunner(Round(text=("hi",), usage=None))
    done = terminal(await collect(runtime(fake)))
    assert isinstance(done, ev.AgentCompleted)
    assert done.usage["num_turns"] == 1
    assert "input_tokens" not in done.usage


@pytest.mark.anyio
async def test_an_answer_that_never_streamed_is_still_reported(
    credentials: None,
) -> None:
    """A host that streams no text deltas still produced an answer the run assembled.
    `streamed=False` is what makes the renderer print it."""

    fake = FakeRunner(Round(text=(), final="assembled, not streamed"))
    events = await collect(runtime(fake))

    assert not [e for e in events if isinstance(e, ev.AssistantToken)]
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "assembled, not streamed"
    assert done.streamed is False


@pytest.mark.anyio
async def test_an_empty_earlier_answer_is_never_sent_back(credentials: None) -> None:
    """A turn that completed with no text records an empty assistant message, and a
    host that rejects a non-final one would fail every later question in the session."""
    fake = FakeRunner(Round(text=("here it is.",)))
    history = [
        Message(role="user", text="a"),
        Message(role="assistant", text=""),
        Message(role="user", text="b"),
    ]
    events = [
        event
        async for event in runtime(fake).stream(
            history, [], emitter=ev.Emitter(uuid4())
        )
    ]
    assert isinstance(terminal(events), ev.AgentCompleted)
    assert [m["content"] for m in fake.runs[0]["input"]] == ["a", "b"]


# -- tools ------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_tool_call_is_routed_paired_and_answered(credentials: None) -> None:
    """The whole point of this runtime: the loop is the framework's and the execution is
    still ours, so our router runs and the model sees the result."""
    fake = FakeRunner(
        Round(tool_calls=(("call_1", "design_wiki_get", '{"key": "gist"}'),)),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=_router)

    started = next(e for e in events if isinstance(e, ev.ToolStarted))
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert started.tool == "wiki_get" and started.source_id == "design"
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, never guessed
    assert result.ok and result.parent_id == started.event_id
    assert result.output == "design/wiki_get says yes"
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_the_runner_is_what_calls_the_tool(credentials: None) -> None:
    """The seam, asserted directly. If the runtime ran tools itself the events would
    look identical and the whole point of the module would be gone."""
    fake = FakeRunner(
        Round(tool_calls=(("call_1", "design_wiki_get", '{"key": "gist"}'),)),
        Round(text=("done",)),
    )
    await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=_router)
    assert fake.calls == [("design_wiki_get", '{"key": "gist"}')]


@pytest.mark.anyio
async def test_two_calls_in_one_round_are_both_run(credentials: None) -> None:
    fake = FakeRunner(
        Round(
            tool_calls=(
                ("call_1", "design_wiki_get", '{"key": "a"}'),
                ("call_2", "design_wiki_search", '{"query": "b"}'),
            )
        ),
        Round(text=("done",)),
    )
    events = await collect(
        runtime(fake),
        tools=[spec("wiki_get"), spec("wiki_search")],
        call_tool=_router,
    )
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 2
    assert len(fake.calls) == 2


@pytest.mark.anyio
async def test_a_failing_tool_reaches_the_model_as_an_error_not_a_dead_turn(
    credentials: None,
) -> None:
    """A hand-built `FunctionTool` has no failure handler, so a raise would end the run.
    The error is returned instead, which makes it the tool's output verbatim — the
    connector's own sentence, because a model told only that "the tool failed" calls it
    again the same way."""

    async def angry(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the store is unreachable")

    fake = FakeRunner(
        Round(tool_calls=(("call_1", "design_wiki_get", "{}"),)),
        Round(text=("I could not read it",)),
    )
    events = await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=angry)
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert not result.ok and result.error == "the store is unreachable"
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_unparseable_arguments_are_never_routed(credentials: None) -> None:
    """The protocol's rule, shared with the chat adapter: a call truncated by the output
    limit arrives as broken JSON, and passing the fragment to a connector would report
    the model's own mistake as a failed source."""
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    fake = FakeRunner(
        Round(tool_calls=(("call_1", "design_wiki_get", '{"key": "gi'),)),
        Round(text=("let me try that again",)),
    )
    events = await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=record)
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert called == [], "a call with unparseable arguments must never be routed"
    assert not result.ok and "valid JSON" in result.error
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_a_tool_the_model_invented_is_told_to_the_model_and_never_routed(
    credentials: None,
) -> None:
    """The SDK resolves the name itself, so an invented one never reaches our router
    and no pair is emitted — the `sdk_anthropic.py` shape. Its default would raise and
    end the turn; the run is configured to hand the model the error instead, because
    one bad call must not end a turn and a model told nothing repeats the call."""
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    fake = FakeRunner(
        Round(tool_calls=(("call_1", "delete_everything", "{}"),)),
        Round(text=("sorry",)),
    )
    events = await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=record)
    assert called == [] and fake.calls == []
    assert not [e for e in events if isinstance(e, ev.ToolResult)]
    config = fake.runs[0]["run_config"]
    assert config.tool_not_found_behavior == "return_error_to_model"
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted) and done.text == "sorry"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "effect", [Effect.LOCAL_WRITE, Effect.EXTERNAL_WRITE, Effect.DESTRUCTIVE]
)
async def test_every_writing_tool_is_withheld_unless_approval_is_auto(
    credentials: None, effect: Effect
) -> None:
    """There is no one to prompt inside a stream, so the offer is the gate."""
    tools = [spec("read"), spec("write", effect)]

    cautious = FakeRunner(Round(text=("hi",)))
    await collect(runtime(cautious), tools=tools)
    assert [t.name for t in cautious.runs[0]["agent"].tools] == ["design_read"]

    permissive = FakeRunner(Round(text=("hi",)))
    await collect(runtime(permissive, approval="auto"), tools=tools)
    assert len(permissive.runs[0]["agent"].tools) == 2


@pytest.mark.anyio
async def test_tool_events_are_yielded_before_the_next_round_s_tokens(
    credentials: None,
) -> None:
    """The relay's whole contract. The tools ran inside the runner, after our last
    yield; held events that came out late — or out of order against the text that
    followed them — would render a turn nobody could read.

    Sequence numbers are checked as non-decreasing rather than strictly increasing: a
    `ToolResult` is stamped by the nested emitter `dispatch` builds, which shares its
    parent's count, so the result and the token after it can carry the same number."""
    fake = FakeRunner(
        Round(
            text=("let me check.",),
            tool_calls=(("call_1", "design_wiki_get", '{"key": "gist"}'),),
        ),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(runtime(fake), tools=[spec("wiki_get")], call_tool=_router)
    assert [type(e).__name__ for e in events] == [
        "AssistantToken",  # "let me check."
        "ToolStarted",
        "ToolResult",
        "AssistantToken",  # the paragraph break
        "AssistantToken",  # "the wiki says yes"
        "AgentCompleted",
    ]
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)

    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "let me check.\n\nthe wiki says yes"


# -- how a turn ends --------------------------------------------------------


@pytest.mark.anyio
async def test_a_model_looping_on_tools_is_stopped_by_the_round_bound(
    credentials: None,
) -> None:
    """A bound, not a budget. Unlike the Anthropic runner, this one raises rather than
    stopping silently — and the SDK drains its queued events before re-raising, so the
    last round's tool pair still has to come out before the terminal event."""
    rounds = [Round(tool_calls=(("c", "design_wiki_get", "{}"),)) for _ in range(3)]
    fake = FakeRunner(*rounds)
    events = await collect(
        runtime(fake, max_tool_rounds=3), tools=[spec("wiki_get")], call_tool=_router
    )
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "tool_rounds"
    assert failed.remedy
    assert len(fake.calls) == 3
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 3


@pytest.mark.anyio
async def test_two_tools_that_fold_to_one_name_end_the_turn_before_a_run(
    credentials: None,
) -> None:
    """The definitions list would carry a duplicate name and the API would answer 400.
    Refused here, so the message names both tools rather than one wire name."""
    fake = FakeRunner(Round(text=("unused",)))
    tools = [
        ToolSpec(name="search", source_id="my wiki", effect=Effect.EXTERNAL_READ),
        ToolSpec(name="search", source_id="my_wiki", effect=Effect.EXTERNAL_READ),
    ]
    failed = terminal(await collect(runtime(fake), tools))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_error"
    assert "my wiki.search" in failed.message
    assert "my_wiki.search" in failed.message
    assert fake.runs == []


# -- what the SDK raises ----------------------------------------------------


@pytest.mark.anyio
async def test_a_rejected_key_names_the_variable_to_check(credentials: None) -> None:
    """The exceptions are the `openai` SDK's, because the framework raises them
    through — one failure ladder, whoever owns the loop."""
    fake = FakeRunner(
        Round(
            raises=openai.AuthenticationError(
                "unauthorized", response=_response(401), body=None
            )
        )
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "auth"
    assert "FOUNDRY_API_KEY" in failed.remedy


@pytest.mark.anyio
async def test_a_404_carries_the_host_s_own_remedy(credentials: None) -> None:
    fake = FakeRunner(
        Round(
            raises=openai.NotFoundError("not found", response=_response(404), body=None)
        )
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "model_not_found"
    assert "deployment" in failed.remedy
    assert "gpt-5-deployment" in failed.message


@pytest.mark.anyio
async def test_rate_limiting_is_its_own_kind_not_a_generic_api_error(
    credentials: None,
) -> None:
    """It subclasses `APIStatusError`, so catching the general case first would have
    buried it — the reason the clauses are ordered most specific first."""
    fake = FakeRunner(
        Round(
            raises=openai.RateLimitError(
                "slow down", response=_response(429), body=None
            )
        )
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "rate_limit"


@pytest.mark.anyio
async def test_a_status_error_carries_the_endpoint_s_own_explanation(
    credentials: None,
) -> None:
    detail = "Unrecognized request argument supplied: max_completion_tokens"
    fake = FakeRunner(
        Round(
            raises=openai.BadRequestError(
                "bad request",
                response=_response(400),
                body={"message": detail, "type": "invalid_request_error"},
            )
        )
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error"
    assert "400" in failed.message and detail in failed.message


@pytest.mark.anyio
async def test_an_unreachable_endpoint_names_where_it_is_configured(
    credentials: None,
) -> None:
    fake = FakeRunner(
        Round(
            raises=openai.APIConnectionError(
                request=httpx.Request("POST", "https://example")
            )
        )
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "connection"
    assert "FOUNDRY_RESOURCE" in failed.remedy


@pytest.mark.anyio
async def test_a_framework_fault_is_reported_as_itself(credentials: None) -> None:
    """An `AgentsException` the ladder does not name lands as `runtime_error` with its
    own message — the honest report for a fault in the loop we handed over."""
    fake = FakeRunner(
        Round(raises=agents.ModelBehaviorError("the model did something"))
    )
    failed = terminal(await collect(runtime(fake)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_error"
    assert "the model did something" in failed.message


@pytest.mark.anyio
async def test_cancellation_closes_the_stream_rather_than_reporting_itself(
    credentials: None,
) -> None:
    """Cancellation derives from BaseException and is deliberately not caught: a
    cancelled turn must not emit a terminal event a frontend would render."""
    import anyio

    fake = FakeRunner(Round(raises=anyio.get_cancelled_exc_class()()))
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await collect(runtime(fake))


# -- the host rows ----------------------------------------------------------


def test_a_row_is_built_with_the_kwargs_it_declares(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rows as `custom` reaches on this protocol, and the same failure if they
    drift: a value that is right and never sent."""
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    OpenAIAgentsRuntime(host="foundry-openai", model="m")._client()
    assert seen["api_key"] == "never-printed-key"
    assert "never-printed-resource" in str(seen["base_url"])
