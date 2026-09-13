"""The `anthropic` runtime: availability, the turn, and every way a turn can end.

No network, no key, no model. The SDK client is replaced by `fixtures/fake_anthropic`,
which is the same shape the real one is — nested async context managers, streamed text
events, then a final message — so the parts most likely to be wrong are the parts under
test. The exceptions are real SDK classes, because catching them in the wrong order is
the mistake that would otherwise ship: authentication, not-found and rate-limit all
subclass `APIStatusError`.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from uuid import uuid4

import anthropic
import pytest

from latent_intel import events as ev
from latent_intel.agent.runtimes.anthropic import ENV_HOST, AnthropicRuntime
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
    runtime: AnthropicRuntime,
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


# -- availability, offline and by name --------------------------------------


def test_a_runtime_with_nothing_set_names_the_variables_it_wants() -> None:
    """`doctor` builds this with no arguments, which is why it has to be constructible
    with none."""
    reason = AnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "ANTHROPIC_FOUNDRY_API_KEY" in reason
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in reason


def test_either_endpoint_variable_satisfies_foundry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resource name and a full base URL are two ways of saying the same thing, and
    demanding both would refuse a correctly configured machine."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://example/anthropic")
    assert AnthropicRuntime().available()


def test_the_anthropic_host_wants_one_variable_not_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AnthropicRuntime(host="anthropic")
    reason = runtime.unavailable_reason()
    assert reason is not None and "ANTHROPIC_API_KEY" in reason
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert runtime.available()


def test_an_unknown_host_names_the_known_ones() -> None:
    """A typo in a project file must not read as a missing credential."""
    reason = AnthropicRuntime(host="bedrock").unavailable_reason()
    assert reason is not None
    assert "bedrock" in reason and "foundry" in reason and "anthropic" in reason


def test_no_credential_value_ever_reaches_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor` output is pasted into support threads. Names only, always."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "sk-sentinel-value")
    reason = AnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "sk-sentinel-value" not in reason


def test_host_selection_is_option_then_environment_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert AnthropicRuntime().host == "foundry"
    monkeypatch.setenv(ENV_HOST, "anthropic")
    assert AnthropicRuntime().host == "anthropic"
    assert AnthropicRuntime(host="foundry").host == "foundry"


def test_a_config_key_this_runtime_does_not_know_is_rejected_by_name() -> None:
    """Ignoring it honoured a file that says the setting is on — `modle:` built a
    runtime running on the default model and said nothing. The keys are named because
    the remedy is to fix or delete them, and a generic refusal says which file to open
    but not which line."""
    with pytest.raises(RuntimeUnavailable) as caught:
        AnthropicRuntime(model="m", nonsense=1, cwd="/tmp")
    message = str(caught.value)
    assert "cwd" in message and "nonsense" in message
    assert "runtimes: anthropic:" in message


def test_both_endpoint_variables_at_once_are_refused_before_the_sdk_refuses_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AsyncAnthropicFoundry` raises `base_url and resource are mutually exclusive`,
    so reporting ✓ here moved that failure to the first question and dropped the
    variable names on the way."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "r")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://example/anthropic")
    reason = AnthropicRuntime().unavailable_reason()
    assert reason is not None
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in reason
    assert "ANTHROPIC_FOUNDRY_BASE_URL" in reason
    assert "only one" in reason


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
        return None if name == "anthropic" else real_find_spec(name, package)

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name == "anthropic":
            raise AssertionError("the anthropic runtime reached for the SDK anyway")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", no_spec)
    monkeypatch.setattr(builtins, "__import__", refuse)

    failed = terminal(await collect(AnthropicRuntime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"
    assert "latent-intel[api]" in failed.message


@pytest.mark.anyio
async def test_an_unusable_runtime_fails_as_an_event_not_an_exception() -> None:
    """A frontend iterating this over a transport has nowhere to catch an exception."""
    failed = terminal(await collect(AnthropicRuntime()))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_unavailable"


# -- the ordinary turn ------------------------------------------------------


@pytest.mark.anyio
async def test_text_streams_then_completes_once(credentials: None) -> None:
    client = FakeClient(Round(text=("Compaction ", "is bounded.")))
    events = await collect(AnthropicRuntime(client_factory=client))

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
    """The API rejects an empty tool list, so the key is omitted rather than sent
    empty. The same rule applies to an absent system prompt."""
    client = FakeClient(Round(text=("hi",)))
    await collect(AnthropicRuntime(client_factory=client))
    assert "tools" not in client.requests[0]
    assert "system" not in client.requests[0]


@pytest.mark.anyio
async def test_attached_sources_reach_the_model_as_a_system_prompt(
    credentials: None,
) -> None:
    from latent_intel.models import Descriptor

    client = FakeClient(Round(text=("hi",)))
    await collect(
        AnthropicRuntime(client_factory=client),
        sources=[Descriptor(id="design", kind="wiki")],
    )
    assert "design (wiki)" in client.requests[0]["system"]


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
            AnthropicRuntime(client_factory=client),
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
    """The whole point of this runtime: our router runs, and the model sees the result
    on the next request."""
    client = FakeClient(
        Round(
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        ),
        Round(text=("the wiki says yes",)),
    )
    events = await collect(
        AnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )

    started = next(e for e in events if isinstance(e, ev.ToolStarted))
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert started.tool == "wiki_get" and started.source_id == "design"
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, never guessed
    assert result.ok and result.parent_id == started.event_id

    sent = client.requests[1]["messages"][-1]
    assert sent["role"] == "user"
    assert sent["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "design/wiki_get says yes",
            "is_error": False,
        }
    ]
    assert isinstance(terminal(events), ev.AgentCompleted)


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
        AnthropicRuntime(client_factory=client),
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
        AnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get"), spec("wiki_search")],
        call_tool=_router,
    )
    assert len([e for e in events if isinstance(e, ev.ToolResult)]) == 2
    assert len(client.requests[1]["messages"][-1]["content"]) == 2


@pytest.mark.anyio
async def test_a_failing_tool_does_not_end_the_turn(credentials: None) -> None:
    """The model has to be told what went wrong in order to do anything else, so the
    failure travels as a `tool_result` with `is_error` rather than as a dead turn."""

    async def angry(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the store is unreachable")

    client = FakeClient(
        Round(tools=(("call_1", "design_wiki_get", {}),), stop_reason="tool_use"),
        Round(text=("I could not read it",)),
    )
    events = await collect(
        AnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=angry,
    )
    result = next(e for e in events if isinstance(e, ev.ToolResult))
    assert not result.ok and result.error == "the store is unreachable"
    sent = client.requests[1]["messages"][-1]["content"][0]
    assert sent["is_error"] and sent["content"] == "the store is unreachable"
    assert isinstance(terminal(events), ev.AgentCompleted)


@pytest.mark.anyio
async def test_a_tool_the_model_invented_is_never_routed(credentials: None) -> None:
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    client = FakeClient(
        Round(tools=(("call_1", "delete_everything", {}),), stop_reason="tool_use"),
        Round(text=("sorry",)),
    )
    events = await collect(
        AnthropicRuntime(client_factory=client),
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
    await collect(AnthropicRuntime(client_factory=cautious), tools=tools)
    assert [t["name"] for t in cautious.requests[0]["tools"]] == ["design_read"]

    permissive = FakeClient(Round(text=("hi",)))
    await collect(
        AnthropicRuntime(client_factory=permissive, approval="auto"), tools=tools
    )
    assert len(permissive.requests[0]["tools"]) == 2


@pytest.mark.anyio
async def test_a_tool_that_declares_no_parameters_is_still_a_legal_definition(
    credentials: None,
) -> None:
    client = FakeClient(Round(text=("hi",)))
    await collect(AnthropicRuntime(client_factory=client), tools=[spec("ping")])
    assert client.requests[0]["tools"][0]["input_schema"] == {
        "type": "object",
        "properties": {},
    }


# -- how a turn ends --------------------------------------------------------


@pytest.mark.anyio
async def test_a_paused_turn_is_resumed_rather_than_reported(
    credentials: None,
) -> None:
    """`pause_turn` is the API asking us to make the request again, not a failure."""
    client = FakeClient(
        Round(text=("thinking",), stop_reason="pause_turn"),
        Round(text=(" done",)),
    )
    done = terminal(await collect(AnthropicRuntime(client_factory=client)))
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
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == stop and failed.remedy


@pytest.mark.anyio
async def test_a_stop_reason_this_build_does_not_know_is_not_a_success(
    credentials: None,
) -> None:
    """Treating an unrecognised reason as `end_turn` would report a truncated answer
    as a complete one."""
    client = FakeClient(Round(text=("...",), stop_reason="something_new"))
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "something_new"


@pytest.mark.anyio
async def test_a_stream_that_ends_with_no_stop_reason_is_not_an_answer(
    credentials: None,
) -> None:
    """A response cut by a proxy returns normally with no stop reason, and treating
    that as success reported whatever arrived before the cut as the whole answer."""
    client = FakeClient(Round(text=("Compaction is",), stop_reason=None))
    events = await collect(AnthropicRuntime(client_factory=client))
    failed = terminal(events)
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "unexpected_stop"
    assert "without a stop reason" in failed.message
    assert not [e for e in events if isinstance(e, ev.AgentCompleted)]


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
    failed = terminal(await collect(AnthropicRuntime(client_factory=client), tools))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "runtime_error"
    assert "my wiki.search" in failed.message
    assert "my_wiki.search" in failed.message
    assert client.requests == []


@pytest.mark.anyio
async def test_an_empty_earlier_answer_is_never_sent_back(
    credentials: None,
) -> None:
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
        async for event in AnthropicRuntime(client_factory=client).stream(
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
        Round(tools=(("c", "design_wiki_get", {}),), stop_reason="tool_use")
        for _ in range(3)
    ]
    client = FakeClient(*rounds)
    failed = terminal(
        await collect(
            AnthropicRuntime(client_factory=client, max_tool_rounds=3),
            tools=[spec("wiki_get")],
            call_tool=_router,
        )
    )
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "tool_rounds"
    assert len(client.requests) == 3


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
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
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
            AnthropicRuntime(client_factory=client, model="claude-sonnet-5-20260101")
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
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed) and failed.kind == "rate_limit"


@pytest.mark.anyio
async def test_any_other_status_reports_the_status(credentials: None) -> None:
    """A body the endpoint sent nothing useful in leaves the status as the whole
    report."""
    client = FakeClient(
        Round(
            raises=anthropic.APIStatusError(
                "boom", response=_response(500), body=None
            )
        )
    )
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error" and "500" in failed.message
    assert failed.message == "the endpoint returned 500"


@pytest.mark.anyio
async def test_a_status_error_carries_the_endpoint_s_own_explanation(
    credentials: None,
) -> None:
    """Both runtimes report a status error the same way, because both SDKs shape the
    exception the same way — and a 400 that names the offending parameter is the whole
    diagnosis."""
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
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "api_error"
    assert "400" in failed.message and detail in failed.message


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
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
    assert isinstance(failed, ev.AgentFailed)
    assert failed.kind == "connection"
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in failed.remedy


@pytest.mark.anyio
async def test_an_error_the_sdk_does_not_own_is_still_one_event(
    credentials: None,
) -> None:
    client = FakeClient(Round(raises=ValueError("something else entirely")))
    failed = terminal(await collect(AnthropicRuntime(client_factory=client)))
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
        await collect(AnthropicRuntime(client_factory=client))


@pytest.mark.anyio
async def test_text_after_a_tool_round_starts_a_new_paragraph(
    credentials: None,
) -> None:
    """Text before a call and text after it are separate blocks. Run straight together
    the answer reads "let me check.The wiki says"; a paused turn, by contrast, resumes
    mid-sentence and must not get a break."""
    client = FakeClient(
        Round(
            text=("let me check.",),
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        ),
        Round(text=("the wiki",), stop_reason="pause_turn"),
        Round(text=(" says yes",)),
    )
    events = await collect(
        AnthropicRuntime(client_factory=client),
        tools=[spec("wiki_get")],
        call_tool=_router,
    )
    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted)
    assert done.text == "let me check.\n\nthe wiki says yes"
    streamed = "".join(e.text for e in events if isinstance(e, ev.AssistantToken))
    assert streamed == done.text
