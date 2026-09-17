"""The `sdk-anthropic` runtime: availability, the turn, and every way a turn can end.

`test_anthropic_runtime.py` case for case, because the promise of this runtime is that
it answers exactly as its sibling does with someone else driving the loop — a case that
passes there and not here is a difference no one asked for. What is new is the seam:
the runner calls our tools, the events they produce are relayed, and the ceiling,
caching and iteration bound are parameters rather than a loop we wrote.

No network, no key, no model. The client is `fixtures/fake_anthropic`, whose
`beta.messages.tool_runner` mirrors `BaseAsyncToolRunner.__run__` over the same scripted
rounds the other path uses.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from uuid import uuid4

import anthropic
import pytest

from latent_intel import events as ev
from latent_intel.agent.runtimes.sdk_anthropic import ENV_HOST, SdkAnthropicRuntime
from latent_intel.models import Effect, Message, RuntimeUnavailable, ToolSpec
from tests.fixtures.fake_anthropic import FakeClient, Round, Usage

try:  # the installed SDK (1.x) builds its errors from httpx2; the 0.x line used httpx
    import httpx2 as httpx
except ModuleNotFoundError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Foundry's two variables, present but meaningless. `conftest` deletes every
    `ANTHROPIC_*` first, so this is the only reason they exist in a test."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "never-printed-key")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "never-printed-resource")


def spec(name: str, effect: Effect = Effect.EXTERNAL_READ) -> ToolSpec:
    return ToolSpec(
        name=name, source_id="design", description=f"the {name} tool", effect=effect
    )


async def collect(
    runtime: SdkAnthropicRuntime,
    tools: list[ToolSpec] | None = None,
    **options: Any,
) -> list[ev.AgentEvent]:
    return [
        event
        async for event in runtime.stream(
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


def results_of(client: FakeClient, request: int) -> list[dict[str, Any]]:
    """The tool results the runner appended before `request`.

    The difference from the sibling suite: the results message is assembled by the
    runner, not by us, so this reads what the SDK would have sent rather than what we
    would have built."""
    content = client.requests[request]["messages"][-1]["content"]
    assert isinstance(content, list)
    return content


# -- availability, offline and by name --------------------------------------


def test_a_runtime_with_nothing_set_names_the_variables_it_wants() -> None:
    """`doctor` builds this with no arguments, which is why it has to be constructible
    with none."""
    reason = SdkAnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "ANTHROPIC_FOUNDRY_API_KEY" in reason
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in reason


def test_either_endpoint_variable_satisfies_foundry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://example/anthropic")
    assert SdkAnthropicRuntime().available()


def test_the_anthropic_host_wants_one_variable_not_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SdkAnthropicRuntime(host="anthropic")
    reason = runtime.unavailable_reason()
    assert reason is not None and "ANTHROPIC_API_KEY" in reason
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert runtime.available()


def test_an_unknown_host_names_the_known_ones() -> None:
    reason = SdkAnthropicRuntime(host="bedrock").unavailable_reason()
    assert reason is not None
    assert (
        "bedrock" in reason and "foundry-anthropic" in reason and "anthropic" in reason
    )


def test_no_credential_value_ever_reaches_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor` output is pasted into support threads. Names only, always."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "sk-sentinel-value")
    reason = SdkAnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "sk-sentinel-value" not in reason


def test_host_selection_reads_this_runtime_s_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its own variable, not the sibling's: a machine comparing the two loops sets one
    against Foundry and the other against the public API."""
    assert SdkAnthropicRuntime().host == "foundry-anthropic"
    monkeypatch.setenv("LATENT_INTEL_ANTHROPIC_HOST", "anthropic")
    assert SdkAnthropicRuntime().host == "foundry-anthropic"
    monkeypatch.setenv(ENV_HOST, "anthropic")
    assert SdkAnthropicRuntime().host == "anthropic"
    assert SdkAnthropicRuntime(host="foundry-anthropic").host == "foundry-anthropic"


def test_a_config_key_this_runtime_does_not_know_is_rejected_by_name() -> None:
    """The keys are named because the remedy is to fix or delete them, and the block
    is named because a generic refusal says which file but not which line."""
    with pytest.raises(RuntimeUnavailable) as caught:
        SdkAnthropicRuntime(model="m", nonsense=1, cwd="/tmp")
    message = str(caught.value)
    assert "cwd" in message and "nonsense" in message
    assert "runtimes: sdk-anthropic:" in message


def test_both_endpoint_variables_at_once_are_refused_before_the_sdk_refuses_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "r")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://example/anthropic")
    reason = SdkAnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in reason
    assert "ANTHROPIC_FOUNDRY_BASE_URL" in reason
    assert "only one" in reason


def test_an_empty_resource_is_named_rather_than_read_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "")
    monkeypatch.setenv(
        "ANTHROPIC_FOUNDRY_BASE_URL", "https://private.example/anthropic"
    )
    reason = SdkAnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in reason
    assert "empty" in reason


@pytest.mark.anyio
async def test_a_base_install_gets_an_event_naming_the_extra_not_an_ImportError(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    """The guarantee for an install without `[api]`: `find_spec` answers first, so
    neither the SDK nor its tool-runner helpers are reached."""
    import builtins

    real_import = builtins.__import__
    real_find_spec = importlib.util.find_spec

    def no_spec(name: str, package: str | None = None) -> Any:
        return None if name == "anthropic" else real_find_spec(name, package)

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("anthropic"):
            raise AssertionError("the sdk-anthropic runtime reached for the SDK anyway")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", no_spec)
    monkeypatch.setattr(builtins, "__import__", refuse)

    failed = terminal(await collect(SdkAnthropicRuntime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"
    assert "latent-intel[api]" in failed.message


@pytest.mark.anyio
async def test_an_unusable_runtime_fails_as_an_event_not_an_exception() -> None:
    """A frontend iterating this over a transport has nowhere to catch an exception."""
    failed = terminal(await collect(SdkAnthropicRuntime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"


def test_this_runtime_declares_who_owns_its_loop() -> None:
    """Declared, never inferred — `doctor` groups on this."""
    assert SdkAnthropicRuntime().family() == "sdk"


# -- the ordinary turn ------------------------------------------------------


@pytest.mark.anyio
async def test_text_streams_then_completes_once(credentials: None) -> None:
    client = FakeClient(Round(text=("Compaction ", "is bounded.")))
    events = await collect(SdkAnthropicRuntime(client_factory=client))

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
async def test_a_turn_with_no_tools_sends_no_tools_key(credentials: None) -> None:
    """`tool_runner` requires `tools=`, so an empty turn cannot simply omit it the way
    the sibling does — the key is dropped from the params the runner makes its requests
    from instead. The API rejects an empty tool list, and a turn with no sources
    attached is the common case, not an edge one."""
    client = FakeClient(Round(text=("hi",)))
    await collect(SdkAnthropicRuntime(client_factory=client))
    assert "tools" not in client.requests[0]
    assert "system" not in client.requests[0]


@pytest.mark.anyio
async def test_attached_sources_reach_the_model_as_a_system_prompt(
    credentials: None,
) -> None:
    from latent_intel.models import Descriptor

    client = FakeClient(Round(text=("hi",)))
    await collect(
        SdkAnthropicRuntime(client_factory=client),
        sources=[Descriptor(id="design", kind="wiki")],
    )
    assert "design (wiki)" in client.requests[0]["system"]


@pytest.mark.anyio
async def test_caching_is_on_and_the_ceiling_is_the_runner_s(credentials: None) -> None:
    """The two parameters this runtime exists to get for free: an ephemeral cache
    breakpoint, which is the largest cost lever here, and a bound the runner enforces
    rather than a `for` loop we wrote."""
    client = FakeClient(Round(text=("hi",)))
    await collect(SdkAnthropicRuntime(client_factory=client, max_tool_rounds=4))
    assert client.requests[0]["cache_control"] == {"type": "ephemeral"}
    assert client.requests[0]["max_iterations"] == 4


@pytest.mark.anyio
async def test_usage_is_summed_across_rounds_not_taken_from_the_last(
    credentials: None,
) -> None:
    """A tool-heavy question is several round-trips; the last one's usage is a small
    fraction of what it cost."""
    client = FakeClient(
        Round(
            tools=(("t1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
            usage=Usage(input_tokens=100, output_tokens=20),
        ),
        Round(text=("done",), usage=Usage(input_tokens=200, output_tokens=5)),
    )
    done = terminal(
        await collect(
            SdkAnthropicRuntime(client_factory=client),
            tools=[spec("wiki_get")],
            call_tool=_router,
        )
    )
    assert isinstance(done, ev.AgentCompleted)
    assert done.usage["input_tokens"] == 300
    assert done.usage["output_tokens"] == 25
    assert done.usage["num_turns"] == 2
    assert "duration_ms" in done.usage


# -- tools ------------------------------------------------------------------


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    return f"{source_id}/{name} says yes"


@pytest.mark.anyio
async def test_a_tool_call_is_routed_paired_and_sent_back(credentials: None) -> None:
    """The whole point of this runtime: the loop is the SDK's and the execution is
    still ours, so our router runs and the model sees the result on the next request."""
    client = FakeClient(
        Round(
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        ),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )

    started = next(e for e in events if isinstance(e, ev.ToolStarted))
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert started.tool == "wiki_get" and started.source_id == "design"
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, never guessed
    assert result.ok and result.parent_id == started.event_id

    assert client.requests[1]["messages"][-1]["role"] == "user"
    assert results_of(client, 1) == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "design/wiki_get says yes",
        }
    ]
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_the_runner_is_what_calls_the_tool(credentials: None) -> None:
    """The seam, asserted directly. If the runtime ran tools itself the events would
    look identical and the whole point of the module would be gone."""
    client = FakeClient(
        Round(
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        ),
        Round(text=("done",)),
    )
    await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )
    assert client.calls == [("design_wiki_get", {"key": "gist"})]


@pytest.mark.anyio
async def test_the_assistant_blocks_go_back_verbatim(credentials: None) -> None:
    """A thinking block returned without its signature is rejected on the next
    request, so the content list is appended as it arrived rather than rebuilt."""
    client = FakeClient(
        Round(
            thinking="let me look",
            tools=(("call_1", "design_wiki_get", {}),),
            stop_reason="tool_use",
        ),
        Round(text=("done",)),
    )
    await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )
    blocks = client.requests[1]["messages"][1]["content"]
    assert [b.type for b in blocks] == ["thinking", "tool_use"]
    assert blocks[0].signature == "signature"


@pytest.mark.anyio
async def test_two_tool_blocks_in_one_message_are_both_run(credentials: None) -> None:
    client = FakeClient(
        Round(
            tools=(
                ("call_1", "design_wiki_get", {"key": "a"}),
                ("call_2", "design_wiki_search", {"query": "b"}),
            ),
            stop_reason="tool_use",
        ),
        Round(text=("done",)),
    )
    events = await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get"), spec("wiki_search")],
        call_tool=_router,
    )
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 2
    assert len(results_of(client, 1)) == 2


@pytest.mark.anyio
async def test_a_failing_tool_reaches_the_model_as_an_error_not_a_dead_turn(
    credentials: None,
) -> None:
    """The bridge raises `BridgeError`, the runtime turns it into the SDK's `ToolError`
    and the runner writes a `tool_result` with `is_error`. The connector's own sentence
    survives all three hops, because a model told only that "the tool failed" calls it
    again the same way."""

    async def angry(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the store is unreachable")

    client = FakeClient(
        Round(tools=(("call_1", "design_wiki_get", {}),), stop_reason="tool_use"),
        Round(text=("I could not read it",)),
    )
    events = await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=angry,
    )
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert not result.ok and result.error == "the store is unreachable"
    sent = results_of(client, 1)[0]
    assert sent["is_error"] and sent["content"] == "the store is unreachable"
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_a_tool_the_model_invented_is_never_routed(credentials: None) -> None:
    """The runner resolves the name against the tools it was given, so an invented one
    never reaches a tool object at all — and therefore never reaches our router."""
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    client = FakeClient(
        Round(tools=(("call_1", "delete_everything", {}),), stop_reason="tool_use"),
        Round(text=("sorry",)),
    )
    events = await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=record,
    )
    assert called == [] and client.calls == []
    assert not [e for e in events if isinstance(e, ev.ToolResult)]
    assert results_of(client, 1)[0]["is_error"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "effect", [Effect.LOCAL_WRITE, Effect.EXTERNAL_WRITE, Effect.DESTRUCTIVE]
)
async def test_every_writing_tool_is_withheld_unless_approval_is_auto(
    credentials: None, effect: Effect
) -> None:
    """There is no one to prompt inside a stream, so the offer is the gate."""
    tools = [spec("read"), spec("write", effect)]

    cautious = FakeClient(Round(text=("hi",)))
    await collect(SdkAnthropicRuntime(client_factory=cautious), tools=tools)
    assert [t["name"] for t in cautious.requests[0]["tools"]] == ["design_read"]

    permissive = FakeClient(Round(text=("hi",)))
    await collect(
        SdkAnthropicRuntime(client_factory=permissive, approval="auto"), tools=tools
    )
    assert len(permissive.requests[0]["tools"]) == 2


@pytest.mark.anyio
async def test_a_tool_that_declares_no_parameters_is_still_a_legal_definition(
    credentials: None,
) -> None:
    client = FakeClient(Round(text=("hi",)))
    await collect(SdkAnthropicRuntime(client_factory=client), tools=[spec("ping")])
    assert client.requests[0]["tools"][0]["input_schema"] == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.anyio
async def test_tool_events_are_yielded_before_the_next_round_s_tokens(
    credentials: None,
) -> None:
    """The relay's whole contract. The tools ran inside the runner, after our last
    yield; held events that came out late — or out of order against the text that
    followed them — would render a turn nobody could read.

    Sequence numbers are checked as non-decreasing rather than strictly increasing: a
    `ToolResult` is stamped by the nested emitter `dispatch` builds, which shares its
    parent's count, so the result and the token after it can carry the same number.
    Position in the stream is the guarantee."""
    client = FakeClient(
        Round(
            text=("let me check.",),
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        ),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(
        SdkAnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
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
async def test_a_paused_turn_is_resumed_by_the_runner_rather_than_reported(
    credentials: None,
) -> None:
    """`pause_turn` is the API asking for the request again, and the runner makes it —
    so the classification below never sees the reason at all."""
    client = FakeClient(
        Round(text=("thinking",), stop_reason="pause_turn"),
        Round(text=(" done",)),
    )
    done = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "thinking done"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop", ["max_tokens", "refusal", "model_context_window_exceeded"]
)
async def test_a_stop_reason_that_is_not_an_answer_fails_with_a_remedy(
    credentials: None, stop: str
) -> None:
    client = FakeClient(Round(text=("half an ans",), stop_reason=stop))
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == stop and failed.remedy


@pytest.mark.anyio
async def test_a_stop_reason_this_build_does_not_know_is_not_a_success(
    credentials: None,
) -> None:
    """The runner exits silently on a reason it does not classify, so reading the last
    message is the only thing standing between that and a truncated answer reported as
    a complete one."""
    client = FakeClient(Round(text=("...",), stop_reason="something_new"))
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "something_new"


@pytest.mark.anyio
async def test_a_stream_that_ends_with_no_stop_reason_is_not_an_answer(
    credentials: None,
) -> None:
    client = FakeClient(Round(text=("Compaction is",), stop_reason=None))
    events = await collect(SdkAnthropicRuntime(client_factory=client))
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "unexpected_stop"
    assert "without a stop reason" in failed.message
    assert not [e for e in events if isinstance(e, ev.AgentCompleted)]


@pytest.mark.anyio
async def test_a_runner_that_makes_no_request_at_all_is_not_an_answer(
    credentials: None,
) -> None:
    """`max_iterations` of zero, or any runner that stops before its first request:
    there is no last message to classify, and an empty answer is not one."""
    client = FakeClient(Round(text=("unused",)))
    failed = terminal(
        await collect(SdkAnthropicRuntime(client_factory=client, max_tool_rounds=0))
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "unexpected_stop"
    assert client.requests == []


@pytest.mark.anyio
async def test_two_tools_that_fold_to_one_name_end_the_turn_before_a_request(
    credentials: None,
) -> None:
    """The definitions list would carry a duplicate name and the API would answer 400.
    Refused here, so the message names both tools rather than one wire name."""
    client = FakeClient(Round(text=("unused",)))
    tools = [
        ToolSpec(name="search", source_id="my wiki", effect=Effect.EXTERNAL_READ),
        ToolSpec(name="search", source_id="my_wiki", effect=Effect.EXTERNAL_READ),
    ]
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client), tools))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_error"
    assert "my wiki.search" in failed.message
    assert "my_wiki.search" in failed.message
    assert client.requests == []


@pytest.mark.anyio
async def test_an_empty_earlier_answer_is_never_sent_back(credentials: None) -> None:
    """A turn that completed with no text records an empty assistant message, and the
    API rejects a non-final one with empty content — so every later question in that
    session failed over a turn that had already ended."""
    client = FakeClient(Round(text=("here it is.",)))
    history = [
        Message(role="user", text="a"),
        Message(role="assistant", text=""),
        Message(role="user", text="b"),
    ]
    events = [
        event
        async for event in SdkAnthropicRuntime(client_factory=client).stream(
            history, [], emitter=ev.Emitter(uuid4())
        )
    ]
    assert isinstance(terminal(events), ev.AgentCompleted)
    sent = client.requests[0]["messages"]
    assert [m["content"] for m in sent] == ["a", "b"]


@pytest.mark.anyio
async def test_a_model_looping_on_tools_is_stopped_by_the_round_bound(
    credentials: None,
) -> None:
    """A bound, not a budget. The runner exits silently when `max_iterations` is
    reached, so the ceiling is read off a last message still asking for tools — and the
    tools of that last round ran inside the runner, whose events still have to come
    out."""
    rounds = [
        Round(tools=(("c", "design_wiki_get", {}),), stop_reason="tool_use")
        for _ in range(3)
    ]
    client = FakeClient(*rounds)
    events = await collect(
        SdkAnthropicRuntime(client_factory=client, max_tool_rounds=3),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "tool_rounds"
    assert failed.remedy
    assert len(client.requests) == 3
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 3


# -- what the SDK raises ----------------------------------------------------


@pytest.mark.anyio
async def test_a_rejected_key_names_the_variable_to_check(credentials: None) -> None:
    client = FakeClient(
        Round(
            raises=anthropic.AuthenticationError(
                "unauthorized", response=_response(401), body=None
            )
        )
    )
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "auth"
    assert "ANTHROPIC_FOUNDRY_API_KEY" in failed.remedy


@pytest.mark.anyio
async def test_a_404_carries_the_host_s_own_remedy(credentials: None) -> None:
    """Foundry resolves deployment names, so a dated model id 404s there and nowhere
    else. One shared sentence would be wrong for one of the two hosts."""
    client = FakeClient(
        Round(
            raises=anthropic.NotFoundError(
                "not found", response=_response(404), body=None
            )
        )
    )
    failed = terminal(
        await collect(
            SdkAnthropicRuntime(client_factory=client, model="claude-sonnet-5-20260101")
        )
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "model_not_found"
    assert "deployment names" in failed.remedy
    assert "claude-sonnet-5-20260101" in failed.message


@pytest.mark.anyio
async def test_rate_limiting_is_its_own_kind_not_a_generic_api_error(
    credentials: None,
) -> None:
    """It subclasses `APIStatusError`, so catching the general case first would have
    buried it — the reason the clauses are ordered most specific first."""
    client = FakeClient(
        Round(
            raises=anthropic.RateLimitError(
                "slow down", response=_response(429), body=None
            )
        )
    )
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "rate_limit"


@pytest.mark.anyio
async def test_a_status_error_carries_the_endpoint_s_own_explanation(
    credentials: None,
) -> None:
    detail = "max_tokens: must be greater than 0"
    client = FakeClient(
        Round(
            raises=anthropic.BadRequestError(
                "bad request",
                response=_response(400),
                body={"error": {"message": detail}},
            )
        )
    )
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error"
    assert "400" in failed.message and detail in failed.message


@pytest.mark.anyio
async def test_any_other_status_reports_the_status(credentials: None) -> None:
    client = FakeClient(
        Round(
            raises=anthropic.APIStatusError("boom", response=_response(500), body=None)
        )
    )
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error" and failed.message == "the endpoint returned 500"


@pytest.mark.anyio
async def test_an_unreachable_endpoint_names_where_it_is_configured(
    credentials: None,
) -> None:
    client = FakeClient(
        Round(
            raises=anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://example")
            )
        )
    )
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "connection"
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in failed.remedy


@pytest.mark.anyio
async def test_an_error_the_sdk_does_not_own_is_still_one_event(
    credentials: None,
) -> None:
    client = FakeClient(Round(raises=ValueError("something else entirely")))
    failed = terminal(await collect(SdkAnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_error"
    assert "something else entirely" in failed.message


@pytest.mark.anyio
async def test_cancellation_closes_the_stream_rather_than_reporting_itself(
    credentials: None,
) -> None:
    """Cancellation derives from BaseException and is deliberately not caught: a
    cancelled turn must not emit a terminal event a frontend would render."""
    import anyio

    client = FakeClient(Round(raises=anyio.get_cancelled_exc_class()()))
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await collect(SdkAnthropicRuntime(client_factory=client))


# -- the host rows ----------------------------------------------------------


def built(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: str = "AsyncAnthropicFoundry",
    **options: Any,
) -> dict[str, Any]:
    """The kwargs one row's client is actually constructed with. Same rows as the
    sibling runtime, and the same failure if they drift: a value that is right and
    never sent."""
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(anthropic, client, factory)
    SdkAnthropicRuntime(**options)._client()
    return seen


def test_foundry_is_built_with_a_credential_and_an_address_and_nothing_else(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert built(monkeypatch) == {"api_key": "never-printed-key", "base_url": None}

    monkeypatch.delenv("ANTHROPIC_FOUNDRY_RESOURCE")
    monkeypatch.setenv(
        "ANTHROPIC_FOUNDRY_BASE_URL", "https://private.example/anthropic"
    )
    assert built(monkeypatch) == {
        "api_key": "never-printed-key",
        "base_url": "https://private.example/anthropic",
    }


def test_the_anthropic_row_is_built_with_no_base_url_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None, not an empty string: the SDK has its own default and must be left to use
    it, which is a thing only the constructed client can show."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = built(monkeypatch, client="AsyncAnthropic", host="anthropic")
    assert seen == {"api_key": "k", "base_url": None}
