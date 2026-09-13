"""The `openai` runtime: availability, the turn, and every way a turn can end.

No network, no key, no model. The SDK client is replaced by `fixtures/fake_openai`,
which is the same shape the real one is — an awaited `create` returning an async
iterator of chunks, tool calls arriving as fragments, usage on a chunk with no choice —
so the parts most likely to be wrong are the parts under test. The exceptions are real
SDK classes, because catching them in the wrong order is the mistake that would
otherwise ship: authentication, not-found and rate-limit all subclass `APIStatusError`.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from uuid import uuid4

import openai
import pytest

from latent_intel import events as ev
from latent_intel.agent.runtimes.openai import (
    ENV_HOST,
    HOSTS,
    OpenAIRuntime,
    base_url,
)
from latent_intel.models import Effect, Message, RuntimeUnavailable, ToolSpec
from tests.fixtures.fake_openai import (
    FakeClient,
    PromptTokensDetails,
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


def runtime(**options: Any) -> OpenAIRuntime:
    """The host this branch was written for, plus the model it has no default for."""
    options.setdefault("host", "foundry")
    options.setdefault("model", "gpt-5-deployment")
    return OpenAIRuntime(**options)


def spec(name: str, effect: Effect = Effect.EXTERNAL_READ) -> ToolSpec:
    return ToolSpec(
        name=name, source_id="design", description=f"the {name} tool", effect=effect
    )


async def collect(
    backend: OpenAIRuntime,
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


# -- availability, offline and by name --------------------------------------


def test_a_runtime_with_nothing_set_names_the_variable_it_wants() -> None:
    """`doctor` builds this with no arguments, which is why it has to be constructible
    with none."""
    reason = OpenAIRuntime().unavailable_reason()
    assert reason is not None and "OPENAI_API_KEY" in reason


def test_the_foundry_host_names_both_its_variables_and_their_stand_ins() -> None:
    """One Foundry resource has one key, so a deployment that already reaches the
    Anthropic surface is finished — and the reason has to say so, or someone sets a
    second variable to the same value they already have."""
    reason = OpenAIRuntime(host="foundry").unavailable_reason()
    assert reason is not None
    assert "FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY)" in reason
    assert "FOUNDRY_RESOURCE (or ANTHROPIC_FOUNDRY_RESOURCE)" in reason
    assert "FOUNDRY_BASE_URL" in reason


def test_the_anthropic_surface_variables_alone_satisfy_foundry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback, exercised rather than described: the same resource, the same key,
    one surface further along."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "r")
    assert OpenAIRuntime(host="foundry", model="gpt-5-deployment").available()


def test_the_other_surfaces_base_url_is_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ANTHROPIC_FOUNDRY_BASE_URL` points at the Anthropic surface of the same
    resource. Accepting it here would send OpenAI requests to it."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://example/anthropic")
    reason = OpenAIRuntime(host="foundry", model="m").unavailable_reason()
    assert reason is not None and "FOUNDRY_RESOURCE" in reason


def test_either_endpoint_variable_satisfies_foundry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resource name and a full base URL are two ways of saying the same thing, and
    demanding both would refuse a correctly configured machine."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("FOUNDRY_BASE_URL", "https://example/openai/v1")
    assert OpenAIRuntime(host="foundry", model="m").available()


def test_the_openai_host_wants_one_variable_not_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK has a default endpoint, so `OPENAI_BASE_URL` is an override rather than
    a requirement."""
    backend = OpenAIRuntime(host="openai", model="gpt-5")
    reason = backend.unavailable_reason()
    assert reason is not None and "OPENAI_API_KEY" in reason
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert backend.available()


def test_a_configured_runtime_with_no_model_says_which_key_to_set(
    credentials: None,
) -> None:
    """There is no default model: every host names its models differently, so a default
    would be right on at most one of them. `doctor` has to say that offline rather than
    report a runtime as ready that fails at the first question."""
    reason = OpenAIRuntime(host="foundry").unavailable_reason()
    assert reason is not None and "model:" in reason and "openai" in reason


def test_an_unknown_host_names_the_known_ones() -> None:
    """A typo in a project file must not read as a missing credential."""
    reason = OpenAIRuntime(host="openroutr").unavailable_reason()
    assert reason is not None
    assert "openroutr" in reason and "foundry" in reason and "openai" in reason


def test_no_credential_value_ever_reaches_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor` output is pasted into support threads. Names only, always."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "sk-sentinel-value")
    reason = OpenAIRuntime(host="foundry").unavailable_reason()
    assert reason is not None
    assert "sk-sentinel-value" not in reason


def test_host_selection_is_option_then_environment_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert OpenAIRuntime().host == "openai"
    monkeypatch.setenv(ENV_HOST, "foundry")
    assert OpenAIRuntime().host == "foundry"
    assert OpenAIRuntime(host="openai").host == "openai"


def test_a_config_key_this_runtime_does_not_know_is_rejected_by_name() -> None:
    """Ignoring it honoured a file that says the setting is on — `modle:` built a
    runtime that then reported no model was set at all. The keys are named because the
    remedy is to fix or delete them, and a generic refusal says which file to open but
    not which line."""
    with pytest.raises(RuntimeUnavailable) as caught:
        OpenAIRuntime(model="m", nonsense=1, cwd="/tmp")
    message = str(caught.value)
    assert "cwd" in message and "nonsense" in message
    assert "runtimes: openai:" in message


def test_both_endpoint_variables_at_once_are_reported_rather_than_silently_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`base_url` prefers the resource, so the other one is doing nothing while the
    file says it is the endpoint. The Anthropic SDK refuses the same pair outright, and
    a runtime that disagreed with its sibling about this would be the worse answer."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("FOUNDRY_RESOURCE", "r")
    monkeypatch.setenv("FOUNDRY_BASE_URL", "https://example/openai/v1")
    reason = OpenAIRuntime(host="foundry", model="m").unavailable_reason()
    assert reason is not None
    assert "FOUNDRY_RESOURCE" in reason and "FOUNDRY_BASE_URL" in reason
    assert "only one" in reason


# -- the ordinary turn ------------------------------------------------------


@pytest.mark.anyio
async def test_text_streams_then_completes_once(credentials: None) -> None:
    client = FakeClient(Round(text=("Compaction ", "is bounded.")))
    events = await collect(runtime(client_factory=client))

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
async def test_the_request_carries_the_token_bound_and_asks_for_usage(
    credentials: None,
) -> None:
    """`max_tokens` is rejected by reasoning models, and without `stream_options` the
    last chunk carries no usage at all — a turn that reports nothing about its cost."""
    client = FakeClient(Round(text=("hi",)))
    await collect(runtime(client_factory=client, max_tokens=4096))
    request = client.requests[0]
    assert request["max_completion_tokens"] == 4096
    assert "max_tokens" not in request
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}


@pytest.mark.anyio
async def test_a_turn_with_no_tools_sends_no_tools_key(credentials: None) -> None:
    """The API rejects an empty tool list, so the key is omitted rather than sent
    empty. With no sources there is no system message either."""
    client = FakeClient(Round(text=("hi",)))
    await collect(runtime(client_factory=client))
    assert "tools" not in client.requests[0]
    assert [m["role"] for m in client.requests[0]["messages"]] == ["user"]


@pytest.mark.anyio
async def test_attached_sources_reach_the_model_as_the_first_message(
    credentials: None,
) -> None:
    """The system prompt is a message on this protocol, and it is first: a later one is
    advice the model has already been talking over."""
    from latent_intel.models import Descriptor

    client = FakeClient(Round(text=("hi",)))
    await collect(
        runtime(client_factory=client),
        sources=[Descriptor(id="design", kind="wiki")],
    )
    first = client.requests[0]["messages"][0]
    assert first["role"] == "system" and "design (wiki)" in first["content"]


@pytest.mark.anyio
async def test_usage_is_mapped_to_our_keys_and_summed_across_rounds(
    credentials: None,
) -> None:
    """This protocol names its counts differently — prompt and completion, with the
    cached share nested. A renderer must not need to know which runtime answered."""
    client = FakeClient(
        Round(
            tool_calls=(("t1", "design_wiki_get", ('{"key": "gist"}',)),),
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=100, completion_tokens=20),
        ),
        Round(
            text=("done",),
            usage=Usage(
                prompt_tokens=200,
                completion_tokens=5,
                prompt_tokens_details=PromptTokensDetails(
                    cached_tokens=80, cache_write_tokens=15
                ),
            ),
        ),
    )
    done = terminal(
        await collect(
            runtime(client_factory=client),
            tools=[spec("wiki_get")],
            call_tool=_router,
        )
    )
    assert isinstance(done, ev.AgentCompleted)
    assert done.usage["input_tokens"] == 300
    assert done.usage["output_tokens"] == 25
    assert done.usage["cache_read_input_tokens"] == 80
    assert done.usage["cache_creation_input_tokens"] == 15
    assert done.usage["num_turns"] == 2
    assert "duration_ms" in done.usage


@pytest.mark.anyio
async def test_a_round_with_no_usage_still_counts_as_a_round(
    credentials: None,
) -> None:
    """A compatible host may send none. Reporting one turn for two round-trips would
    understate the cost of exactly the questions that cost most."""
    client = FakeClient(Round(text=("hi",), usage=None))
    done = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(done, ev.AgentCompleted)
    assert done.usage["num_turns"] == 1
    assert "input_tokens" not in done.usage


# -- tools ------------------------------------------------------------------


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    return f"{source_id}/{name} says yes"


@pytest.mark.anyio
async def test_a_call_split_across_fragments_is_reassembled_routed_and_sent_back(
    credentials: None,
) -> None:
    """The whole point of this runtime, and the trap this protocol adds: the arguments
    arrive as string pieces keyed by index, and a runtime that reads only the first
    fragment calls the tool with half an object."""
    client = FakeClient(
        Round(
            tool_calls=(("call_1", "design_wiki_get", ('{"key"', ': "gi', 'st"}')),),
            finish_reason="tool_calls",
        ),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(
        runtime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )

    started = next(e for e in events if isinstance(e, ev.ToolStarted))
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert started.tool == "wiki_get" and started.source_id == "design"
    assert started.arguments == {"key": "gist"}
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, never guessed
    assert result.ok and result.parent_id == started.event_id

    sent = client.requests[1]["messages"]
    assert sent[-2]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "design_wiki_get", "arguments": '{"key": "gist"}'},
        }
    ]
    assert sent[-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "design/wiki_get says yes",
    }
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_a_tool_definition_is_wrapped_the_way_this_protocol_wants(
    credentials: None,
) -> None:
    """A schema at the top level is what the other protocol takes; here it is nested
    under `function`, and a tool that declares nothing still needs an object schema."""
    client = FakeClient(Round(text=("hi",)))
    await collect(runtime(client_factory=client), tools=[spec("ping")])
    definition = client.requests[0]["tools"][0]
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "design_ping"
    assert definition["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.anyio
async def test_arguments_that_are_not_valid_json_fail_the_call_not_the_turn(
    credentials: None,
) -> None:
    """A model that emitted broken JSON can emit it again correctly; one told nothing
    repeats the call. So the failure travels back as a tool message."""
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    client = FakeClient(
        Round(
            tool_calls=(("call_1", "design_wiki_get", ('{"key": "gi',)),),
            finish_reason="tool_calls",
        ),
        Round(text=("let me try that again",)),
    )
    events = await collect(
        runtime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=record,
    )
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert called == [], "a call with unparseable arguments must never be routed"
    assert not result.ok and "valid JSON" in result.error
    assert "valid JSON" in client.requests[1]["messages"][-1]["content"]
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_two_calls_in_one_round_are_both_run_and_both_answered(
    credentials: None,
) -> None:
    """One tool message per call, each carrying its own id: a round that answers only
    the last of them is rejected on the next request."""
    client = FakeClient(
        Round(
            tool_calls=(
                ("call_1", "design_wiki_get", ('{"key": "a"}',)),
                ("call_2", "design_wiki_search", ('{"query": "b"}',)),
            ),
            finish_reason="tool_calls",
        ),
        Round(text=("done",)),
    )
    events = await collect(
        runtime(client_factory=client),
        tools=[spec("wiki_get"), spec("wiki_search")],
        call_tool=_router,
    )
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 2
    sent = client.requests[1]["messages"]
    assert [m["tool_call_id"] for m in sent[-2:]] == ["call_1", "call_2"]


@pytest.mark.anyio
async def test_a_failing_tool_does_not_end_the_turn(credentials: None) -> None:
    """The model has to be told what went wrong in order to do anything else, so the
    failure travels as the tool message's content rather than as a dead turn."""

    async def angry(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the store is unreachable")

    client = FakeClient(
        Round(
            tool_calls=(("call_1", "design_wiki_get", ("{}",)),),
            finish_reason="tool_calls",
        ),
        Round(text=("I could not read it",)),
    )
    events = await collect(
        runtime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=angry,
    )
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert not result.ok and result.error == "the store is unreachable"
    assert client.requests[1]["messages"][-1]["content"] == "the store is unreachable"
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_a_tool_the_model_invented_is_never_routed(credentials: None) -> None:
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    client = FakeClient(
        Round(
            tool_calls=(("call_1", "delete_everything", ("{}",)),),
            finish_reason="tool_calls",
        ),
        Round(text=("sorry",)),
    )
    events = await collect(
        runtime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=record,
    )
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert called == [] and not result.ok


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
    await collect(runtime(client_factory=cautious), tools=tools)
    assert [t["function"]["name"] for t in cautious.requests[0]["tools"]] == [
        "design_read"
    ]

    permissive = FakeClient(Round(text=("hi",)))
    await collect(runtime(client_factory=permissive, approval="auto"), tools=tools)
    assert len(permissive.requests[0]["tools"]) == 2


# -- how a turn ends --------------------------------------------------------


@pytest.mark.anyio
async def test_running_out_of_output_is_reported_under_the_shared_kind(
    credentials: None,
) -> None:
    """The wire name is `length`; the kind is the one `anthropic` already reports, so a
    frontend switching on it does not need two vocabularies."""
    client = FakeClient(Round(text=("half an ans",), finish_reason="length"))
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "max_tokens" and failed.remedy


@pytest.mark.anyio
async def test_a_filtered_response_says_what_to_do_about_it(
    credentials: None,
) -> None:
    client = FakeClient(Round(finish_reason="content_filter"))
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "content_filter" and failed.remedy == "rephrase the question"


@pytest.mark.anyio
async def test_a_finish_reason_this_build_does_not_know_is_not_a_success(
    credentials: None,
) -> None:
    """Treating an unrecognised reason as `stop` would report a truncated answer as a
    complete one."""
    client = FakeClient(Round(text=("...",), finish_reason="something_new"))
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "unexpected_stop" and "something_new" in failed.message


@pytest.mark.anyio
async def test_a_call_truncated_by_the_output_limit_is_never_dispatched(
    credentials: None,
) -> None:
    """`length` cuts the arguments mid-JSON. Dispatching that reported the model's own
    broken fragment as a failed tool and spent the round bound retrying it, while the
    real reason — the output limit — was never said at all."""
    client = FakeClient(
        Round(
            tool_calls=(("c1", "design_wiki_get", ('{"ref": "wi',)),),
            finish_reason="length",
        )
    )
    events = await collect(
        runtime(client_factory=client), [spec("wiki_get")], call_tool=_router
    )
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "max_tokens"
    assert not [e for e in events if isinstance(e, ev.ToolStarted)]


@pytest.mark.anyio
async def test_asking_for_tools_and_sending_none_ends_the_turn(
    credentials: None,
) -> None:
    """The next round would send an assistant message with neither content nor calls
    and get the same answer back, until the round bound stopped it."""
    client = FakeClient(Round(finish_reason="tool_calls"))
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "unexpected_stop"
    assert "no calls" in failed.message


@pytest.mark.anyio
async def test_a_call_with_no_id_is_given_one_that_both_messages_share(
    credentials: None,
) -> None:
    """Some local servers omit the id. It is only ever a key tying the `tool` message
    back to the assistant's call, and sending `""` twice left the host to guess."""
    client = FakeClient(
        Round(
            tool_calls=((None, "design_wiki_get", ('{"ref": "wiki:compaction"}',)),),
            finish_reason="tool_calls",
        ),
        Round(text=("done.",)),
    )
    events = await collect(
        runtime(client_factory=client), [spec("wiki_get")], call_tool=_router
    )
    assert isinstance(terminal(events), ev.AgentCompleted)
    sent = client.requests[1]["messages"]
    assistant = next(m for m in sent if m.get("tool_calls"))
    answer = next(m for m in sent if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_0"
    assert answer["tool_call_id"] == "call_0"


@pytest.mark.anyio
async def test_a_stream_that_ends_with_no_finish_reason_is_not_an_answer(
    credentials: None,
) -> None:
    """A response cut by a proxy ends with no finish reason, and treating that as
    success reported whatever arrived before the cut as the whole answer."""
    client = FakeClient(Round(text=("Compaction is",), finish_reason=None))
    events = await collect(runtime(client_factory=client))
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "unexpected_stop"
    assert "without a stop reason" in failed.message
    assert not [e for e in events if isinstance(e, ev.AgentCompleted)]


@pytest.mark.anyio
async def test_two_tools_that_fold_to_one_name_end_the_turn_before_a_request(
    credentials: None,
) -> None:
    """A duplicate name in the definitions list is a 400 from the API. Refused here, so
    the message names both tools rather than one wire name."""
    client = FakeClient(Round(text=("unused",)))
    tools = [
        ToolSpec(name="search", source_id="my wiki", effect=Effect.EXTERNAL_READ),
        ToolSpec(name="search", source_id="my_wiki", effect=Effect.EXTERNAL_READ),
    ]
    failed = terminal(await collect(runtime(client_factory=client), tools))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_error"
    assert "my wiki.search" in failed.message
    assert "my_wiki.search" in failed.message
    assert client.requests == []


@pytest.mark.anyio
async def test_an_empty_earlier_answer_is_never_sent_back(
    credentials: None,
) -> None:
    """A turn that completed with no text records an empty assistant message, and a
    host that rejects one would fail every later question in that session over a turn
    that had already ended."""
    client = FakeClient(Round(text=("here it is.",)))
    history = [
        Message(role="user", text="a"),
        Message(role="assistant", text=""),
        Message(role="user", text="b"),
    ]
    events = [
        event
        async for event in runtime(client_factory=client).stream(
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
    """A bound, not a budget: a model calling the same tool forever would otherwise
    spend until someone noticed."""
    rounds = [
        Round(
            tool_calls=(("c", "design_wiki_get", ("{}",)),),
            finish_reason="tool_calls",
        )
        for _ in range(3)
    ]
    client = FakeClient(*rounds)
    failed = terminal(
        await collect(
            runtime(client_factory=client, max_tool_rounds=3),
            tools=[spec("wiki_get")],
            call_tool=_router,
        )
    )
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "tool_rounds"
    assert len(client.requests) == 3


@pytest.mark.anyio
async def test_text_after_a_tool_round_starts_a_new_paragraph(
    credentials: None,
) -> None:
    """Text before a call and text after it are separate blocks. Run straight together
    the answer reads "let me check.The wiki says"."""
    client = FakeClient(
        Round(
            text=("let me check.",),
            tool_calls=(("call_1", "design_wiki_get", ('{"key": "gist"}',)),),
            finish_reason="tool_calls",
        ),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(
        runtime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "let me check.\n\nthe wiki says yes"
    streamed = "".join(e.text for e in events if isinstance(e, ev.AssistantToken))
    assert streamed == done.text


# -- what the SDK raises ----------------------------------------------------


@pytest.mark.anyio
async def test_a_rejected_key_names_the_variable_to_check(credentials: None) -> None:
    client = FakeClient(
        Round(
            raises=openai.AuthenticationError(
                "unauthorized", response=_response(401), body=None
            )
        )
    )
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "auth"
    assert "FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY)" in failed.remedy


@pytest.mark.anyio
async def test_a_404_carries_the_host_s_own_remedy(credentials: None) -> None:
    """Foundry resolves deployment names someone chose; the OpenAI API publishes model
    ids. One shared sentence would be wrong for one of the two hosts."""
    client = FakeClient(
        Round(
            raises=openai.NotFoundError(
                "not found", response=_response(404), body=None
            )
        )
    )
    failed = terminal(
        await collect(runtime(client_factory=client, model="gpt-5-nonesuch"))
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "model_not_found"
    assert "deployment names" in failed.remedy
    assert "gpt-5-nonesuch" in failed.message


@pytest.mark.anyio
async def test_rate_limiting_is_its_own_kind_not_a_generic_api_error(
    credentials: None,
) -> None:
    """It subclasses `APIStatusError`, so catching the general case first would have
    buried it — the reason the clauses are ordered most specific first."""
    client = FakeClient(
        Round(
            raises=openai.RateLimitError(
                "slow down", response=_response(429), body=None
            )
        )
    )
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "rate_limit"


@pytest.mark.anyio
async def test_any_other_status_reports_the_status(credentials: None) -> None:
    """A body the endpoint sent nothing useful in leaves the status as the whole
    report."""
    client = FakeClient(
        Round(
            raises=openai.APIStatusError("boom", response=_response(500), body=None)
        )
    )
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error" and "500" in failed.message
    assert failed.message == "the endpoint returned 500"


@pytest.mark.anyio
async def test_a_status_error_carries_the_endpoint_s_own_explanation(
    credentials: None,
) -> None:
    """The status alone sends someone to a network tab to find out which parameter the
    endpoint disliked; the body already names it."""
    detail = "Unrecognized request argument supplied: max_completion_tokens"
    client = FakeClient(
        Round(
            raises=openai.BadRequestError(
                "bad request",
                response=_response(400),
                body={"error": {"message": detail}},
            )
        )
    )
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error"
    assert "400" in failed.message and detail in failed.message


@pytest.mark.anyio
async def test_an_unreachable_endpoint_names_where_it_is_configured(
    credentials: None,
) -> None:
    client = FakeClient(
        Round(
            raises=openai.APIConnectionError(
                request=httpx.Request("POST", "https://example")
            )
        )
    )
    failed = terminal(await collect(runtime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "connection"
    assert "FOUNDRY_RESOURCE" in failed.remedy


@pytest.mark.anyio
async def test_an_unreachable_default_endpoint_names_the_url_it_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row reached at its row's own URL has no variable to check, and naming one
    nobody set reads as a misconfiguration rather than an outage."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = FakeClient(
        Round(
            raises=openai.APIConnectionError(
                request=httpx.Request("POST", "https://example")
            )
        )
    )
    failed = terminal(
        await collect(
            OpenAIRuntime(
                host="openrouter",
                model="anthropic/claude-sonnet-5",
                client_factory=client,
            )
        )
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "connection"
    assert "https://openrouter.ai/api/v1" in failed.remedy
    assert "OPENROUTER_BASE_URL" not in failed.remedy


@pytest.mark.anyio
async def test_an_error_the_sdk_does_not_own_is_still_one_event(
    credentials: None,
) -> None:
    client = FakeClient(Round(raises=ValueError("something else entirely")))
    failed = terminal(await collect(runtime(client_factory=client)))
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
        await collect(runtime(client_factory=client))


# -- where the client points ------------------------------------------------


def test_foundry_builds_its_base_url_from_the_resource_name(
    credentials: None,
) -> None:
    """A portal shows a resource name; the SDK wants a URL. Asking for both would be
    asking the same question twice."""
    assert base_url(HOSTS["foundry"]) == (
        "https://never-printed-resource.services.ai.azure.com/openai/v1"
    )


def test_an_explicit_base_url_is_used_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sovereign cloud, a private endpoint, a proxy — none of them match the
    template, and the variable exists so none of them need to."""
    monkeypatch.setenv("FOUNDRY_BASE_URL", "https://private.example/openai/v1")
    assert base_url(HOSTS["foundry"]) == "https://private.example/openai/v1"


def test_the_openai_host_points_nowhere_in_particular() -> None:
    """None, not an empty string: the SDK has its own default and must be left to use
    it."""
    assert base_url(HOSTS["openai"]) is None


# -- the host rows ----------------------------------------------------------


def test_openrouter_wants_one_variable_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway publishes its base URL, so a key is the whole configuration — and
    the one thing missing has to be named rather than implied."""
    backend = OpenAIRuntime(host="openrouter", model="anthropic/claude-sonnet-5")
    reason = backend.unavailable_reason()
    assert reason is not None and "OPENROUTER_API_KEY" in reason
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert backend.available()


def test_openrouter_points_at_the_gateway_without_a_variable() -> None:
    """One published URL: requiring a variable for it would be asking every install to
    retype the same string."""
    assert base_url(HOSTS["openrouter"]) == "https://openrouter.ai/api/v1"


def test_openrouters_base_url_variable_still_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy in front of the gateway is the case the variable exists for."""
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example/api/v1")
    assert base_url(HOSTS["openrouter"]) == "https://proxy.example/api/v1"


def test_one_url_field_serves_a_template_and_a_constant_alike(
    credentials: None,
) -> None:
    """`url` is read two ways from one field — formatted where the row's first endpoint
    variable names a resource, verbatim where it names none — and a row that sets it at
    all must not disturb a row that does not."""
    assert base_url(HOSTS["foundry"]).startswith("https://never-printed-resource.")
    assert base_url(HOSTS["openrouter"]) == "https://openrouter.ai/api/v1"
    assert base_url(HOSTS["openai"]) is None


@pytest.mark.anyio
async def test_openrouter_sends_the_parameter_the_gateway_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_completion_tokens` is the OpenAI API's name; the gateway normalises
    `max_tokens` per vendor, which is what it documents."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = FakeClient(Round(text=("hi",)))
    events = await collect(
        OpenAIRuntime(
            host="openrouter",
            model="anthropic/claude-sonnet-5",
            client_factory=client,
            max_tokens=2048,
        )
    )
    assert isinstance(terminal(events), ev.AgentCompleted)
    request = client.requests[0]
    assert request["max_tokens"] == 2048
    assert "max_completion_tokens" not in request


@pytest.mark.anyio
async def test_openrouters_404_names_the_vendor_model_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare `claude-sonnet-5` is the mistake this row invites, and the remedy is the
    only place someone finds out the id carries a vendor."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = FakeClient(
        Round(
            raises=openai.NotFoundError(
                "not found", response=_response(404), body=None
            )
        )
    )
    failed = terminal(
        await collect(
            OpenAIRuntime(
                host="openrouter", model="claude-sonnet-5", client_factory=client
            )
        )
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "model_not_found"
    assert "<vendor>/<model>" in failed.remedy


def test_a_local_server_needs_its_own_address_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the server is, is the one thing nothing can guess — and a key is not
    required, so demanding one would refuse a working install.

    Rewritten from the version that set `OPENAI_BASE_URL`: this row reads its own
    variables, so the public API's address does not configure it.
    """
    backend = OpenAIRuntime(host="local", model="qwen3")
    reason = backend.unavailable_reason()
    assert reason is not None and "LOCAL_OPENAI_BASE_URL" in reason
    assert "LOCAL_OPENAI_API_KEY" not in reason
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    reason = backend.unavailable_reason()
    assert reason is not None and "LOCAL_OPENAI_BASE_URL" in reason
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    assert backend.available()


def test_a_local_server_is_given_the_row_s_own_default_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK refuses to construct a client with no credential at all, even against a
    server that never reads one."""
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    OpenAIRuntime(host="local", model="qwen3")._client()
    assert seen["api_key"] == HOSTS["local"].key_default
    assert seen["base_url"] == "http://localhost:11434/v1"


def test_the_public_api_key_never_reaches_a_local_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason this row has its own variables: `OPENAI_API_KEY` is a credential
    issued for api.openai.com, and forwarding it to whatever is listening on the LAN
    sends it somewhere it was never meant to go."""
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sentinel-value")
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    OpenAIRuntime(host="local", model="qwen3")._client()
    assert "sk-sentinel-value" not in seen.values()
    assert seen["api_key"] == HOSTS["local"].key_default


def test_a_local_server_that_does_check_a_key_is_given_the_real_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vLLM behind an `--api-key` is the case: optional is not the same as ignored."""
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "sk-sentinel-value")
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    OpenAIRuntime(host="local", model="qwen3")._client()
    assert seen["api_key"] == "sk-sentinel-value"


def test_the_openai_and_local_hosts_resolve_from_one_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A laptop that talks to both — the public API for one question and a server on
    the desk for the next — keeps all four variables in one `.env`, and neither row may
    read the other's."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-public")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "sk-lan")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    assert OpenAIRuntime(host="openai", model="gpt-5").available()
    assert OpenAIRuntime(host="local", model="qwen3").available()
    assert base_url(HOSTS["openai"]) == "https://api.openai.com/v1"
    assert base_url(HOSTS["local"]) == "http://localhost:11434/v1"


@pytest.mark.anyio
async def test_a_local_server_answers_with_the_older_token_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_completion_tokens` is newer than several of the servers this row exists
    for, and one that ignores the bound streams until it is stopped."""
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    client = FakeClient(Round(text=("hi",)))
    events = await collect(
        OpenAIRuntime(
            host="local", model="qwen3", client_factory=client, max_tokens=1024
        )
    )
    assert isinstance(terminal(events), ev.AgentCompleted)
    assert client.requests[0]["max_tokens"] == 1024
    assert "max_completion_tokens" not in client.requests[0]


@pytest.mark.anyio
async def test_a_local_404_names_the_server_s_own_model_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local server serves whatever was pulled onto the machine, so only it can say
    what the name is."""
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    client = FakeClient(
        Round(
            raises=openai.NotFoundError(
                "not found", response=_response(404), body=None
            )
        )
    )
    failed = terminal(
        await collect(
            OpenAIRuntime(host="local", model="qwen3", client_factory=client)
        )
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "model_not_found"
    assert "models list" in failed.remedy


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three variables the classic surface needs, present and meaningless."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "never-printed-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://never-printed.openai.azure.com"
    )
    monkeypatch.setenv("OPENAI_API_VERSION", "2026-01-01")


def test_azure_openai_names_all_three_of_its_variables() -> None:
    """Three, not two: this client refuses to be built without a dated API version, and
    someone who set only the pair every other row wants must be told which is left."""
    backend = OpenAIRuntime(host="azure-openai", model="my-deployment")
    reason = backend.unavailable_reason()
    assert reason is not None
    assert "AZURE_OPENAI_API_KEY" in reason
    assert "AZURE_OPENAI_ENDPOINT" in reason
    assert "OPENAI_API_VERSION" in reason


def test_azure_openai_names_the_api_version_when_it_is_the_only_one_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variable nobody expects, and the one whose absence would otherwise surface
    as a 400 from the endpoint naming neither this runtime nor the fix."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    backend = OpenAIRuntime(host="azure-openai", model="my-deployment")
    reason = backend.unavailable_reason()
    assert reason is not None and "OPENAI_API_VERSION" in reason
    assert "AZURE_OPENAI_API_KEY" not in reason
    monkeypatch.setenv("OPENAI_API_VERSION", "2026-01-01")
    assert backend.available()


def test_azure_openai_is_built_with_its_own_client_and_its_own_kwargs(
    azure: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row this hook exists for: a different class, `azure_endpoint` instead of
    `base_url`, and a version the other rows have never heard of."""
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "AsyncAzureOpenAI", factory)
    OpenAIRuntime(host="azure-openai", model="my-deployment")._client()
    assert seen == {
        "api_key": "never-printed-key",
        "azure_endpoint": "https://never-printed.openai.azure.com",
        "api_version": "2026-01-01",
    }
    assert "base_url" not in seen


def built(monkeypatch: pytest.MonkeyPatch, **options: Any) -> dict[str, Any]:
    """The kwargs one row's client is actually constructed with.

    `base_url(host)` alone would not catch a row whose URL never reaches the client,
    which is the failure these rows can have: the value is right and nothing sends it.
    """
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeClient:
        seen.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    OpenAIRuntime(**options)._client()
    return seen


def test_foundry_is_built_with_the_url_its_resource_name_implies(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default construction hook: a key and a base URL, through `AsyncOpenAI`."""
    seen = built(monkeypatch, host="foundry", model="gpt-5-deployment")
    assert set(seen) == {"api_key", "base_url"}
    assert seen["api_key"] == "never-printed-key"
    assert seen["base_url"] == (
        "https://never-printed-resource.services.ai.azure.com/openai/v1"
    )


def test_foundry_is_built_with_the_resource_the_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template is the row's, and the resource is the environment's."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("FOUNDRY_RESOURCE", "demo")
    seen = built(monkeypatch, host="foundry", model="m")
    assert seen["base_url"] == "https://demo.services.ai.azure.com/openai/v1"


def test_openrouter_is_built_with_the_gateway_its_row_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No variable set, and the client still points at the gateway — and an
    `OPENAI_BASE_URL` exported for the row next door does not move it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    seen = built(monkeypatch, host="openrouter", model="anthropic/claude-sonnet-5")
    assert seen["base_url"] == "https://openrouter.ai/api/v1"


def test_openrouters_own_variable_is_what_moves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy in front of the gateway, as far as the client is concerned."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example/api/v1")
    seen = built(monkeypatch, host="openrouter", model="anthropic/claude-sonnet-5")
    assert seen["base_url"] == "https://proxy.example/api/v1"


def test_the_openai_row_is_built_with_no_base_url_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None, not an empty string: the SDK has its own default and must be left to use
    it, which is a thing only the constructed client can show."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    seen = built(monkeypatch, host="openai", model="gpt-5")
    assert seen["base_url"] is None
    assert seen["api_key"] == "k"


@pytest.mark.anyio
async def test_azure_openai_answers_an_ordinary_question(azure: None) -> None:
    """Nothing about the turn differs — which is the claim the row is making.

    Rewritten from the version asserting `max_completion_tokens`: that parameter is
    gated on the dated API version here, so an older `OPENAI_API_VERSION` rejects the
    request outright. `max_tokens` is accepted by every version.
    """
    client = FakeClient(Round(text=("hi",)))
    events = await collect(
        OpenAIRuntime(
            host="azure-openai", model="my-deployment", client_factory=client
        )
    )
    assert isinstance(terminal(events), ev.AgentCompleted)
    assert client.requests[0]["model"] == "my-deployment"
    assert "max_tokens" in client.requests[0]
    assert "max_completion_tokens" not in client.requests[0]


@pytest.mark.anyio
async def test_azure_openais_404_names_the_deployment(azure: None) -> None:
    """A catalogue id pasted in where a deployment name belongs is the mistake, and it
    returns a 404 rather than anything that says so."""
    client = FakeClient(
        Round(
            raises=openai.NotFoundError(
                "not found", response=_response(404), body=None
            )
        )
    )
    failed = terminal(
        await collect(
            OpenAIRuntime(
                host="azure-openai", model="gpt-5-2026-01-01", client_factory=client
            )
        )
    )
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "model_not_found"
    assert "deployment" in failed.remedy


def test_no_azure_credential_value_reaches_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule for every row: `doctor` output is pasted into support threads."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-sentinel-value")
    reason = OpenAIRuntime(host="azure-openai", model="m").unavailable_reason()
    assert reason is not None and "sk-sentinel-value" not in reason


# -- a base install ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_base_install_gets_an_event_naming_the_extra_not_an_ImportError(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    """The guarantee for an install without `[api]`: `find_spec` answers first, so the
    SDK import in `stream` is never reached. Both halves are forced here, because the
    extra *is* installed on a development machine."""
    import builtins

    real_import = builtins.__import__
    real_find_spec = importlib.util.find_spec

    def no_spec(name: str, package: str | None = None) -> Any:
        return None if name == "openai" else real_find_spec(name, package)

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name == "openai":
            raise AssertionError("the openai runtime reached for the SDK anyway")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", no_spec)
    monkeypatch.setattr(builtins, "__import__", refuse)

    backend = runtime()
    reason = backend.unavailable_reason()
    assert reason is not None and "latent-intel[api]" in reason

    failed = terminal(await collect(backend))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"
    assert "latent-intel[api]" in failed.message


@pytest.mark.anyio
async def test_an_unusable_runtime_fails_as_an_event_not_an_exception() -> None:
    """A frontend iterating this over a transport has nowhere to catch an exception."""
    failed = terminal(await collect(OpenAIRuntime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"
