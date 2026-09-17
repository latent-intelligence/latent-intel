"""What is new in `custom`, beyond the two suites it inherited.

`test_custom_messages.py` and `test_custom_chat.py` are the old `anthropic` and
`openai` suites retargeted, and between them they cover the turn on both protocols.
What they cannot cover is what only exists now that one runtime serves both: no default
host, a row that picks the adapter, a model default that comes off the row, and a
setting one protocol takes and the other does not.

No network, no key, no model: the same two fakes, reached through `client_factory`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from latent_intel import events as ev
from latent_intel.agent import hosts
from latent_intel.agent.runtimes.custom import ENV_HOST, CustomRuntime
from latent_intel.models import Effect, Message, ToolSpec
from tests.fixtures import fake_anthropic, fake_openai


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        source_id="design",
        description=f"the {name} tool",
        effect=Effect.EXTERNAL_READ,
    )


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    return f"{source_id}/{name} says yes"


async def collect(
    backend: CustomRuntime, tools: list[ToolSpec] | None = None, **options: Any
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


def terminal(events: list[ev.AgentEvent]) -> ev.AgentEvent:
    ends = [e for e in events if isinstance(e, ev.AgentCompleted | ev.AgentFailed)]
    assert len(ends) == 1, [type(e).__name__ for e in events]
    return ends[0]


# -- choosing a host --------------------------------------------------------


def test_no_host_is_reported_by_name_rather_than_defaulted() -> None:
    """Seven rows across two protocols make any default right for at most one
    deployment. The reason says where to set it and where to see the list, because a
    machine that has not chosen has no way to guess either."""
    reason = CustomRuntime().unavailable_reason()
    assert reason is not None
    assert "no host is set" in reason
    assert "runtimes: custom:" in reason and "intel hosts" in reason


def test_an_unknown_host_names_every_row_this_runtime_can_reach() -> None:
    """`host: foundry` is the mistake the two Foundry rows invite, and the reason is
    the only place someone finds out there are two of them."""
    reason = CustomRuntime(host="foundry").unavailable_reason()
    assert reason is not None and "unknown host 'foundry'" in reason
    for name in hosts.HOSTS:
        assert name in reason, name


def test_the_environment_beats_nothing_and_loses_to_the_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One machine runs against Foundry and another against the public API with the
    same project file checked out on both."""
    assert CustomRuntime().host == ""
    monkeypatch.setenv(ENV_HOST, "openrouter")
    assert CustomRuntime().host == "openrouter"
    assert CustomRuntime(host="anthropic").host == "anthropic"


# -- the row picks the adapter ----------------------------------------------


@pytest.mark.anyio
async def test_a_messages_row_answers_through_the_messages_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tool-using turn on the Anthropic fake: a schema at the top level of the
    definition, and the results back in one user message."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        ),
        fake_anthropic.Round(text=("the wiki says yes",)),
    )
    events = await collect(
        CustomRuntime(host="anthropic", client_factory=client),
        [spec("wiki_get")],
        call_tool=_router,
    )

    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted) and done.text == "the wiki says yes"
    assert "input_schema" in client.requests[0]["tools"][0]
    assert client.requests[1]["messages"][-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "design/wiki_get says yes",
                "is_error": False,
            }
        ],
    }


@pytest.mark.anyio
async def test_a_chat_row_answers_through_the_chat_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tool-using turn on the OpenAI fake: the schema nested under `function`, and
    one `tool` message per call."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = fake_openai.FakeClient(
        fake_openai.Round(
            tool_calls=(("call_1", "design_wiki_get", ('{"key": "gist"}',)),),
            finish_reason="tool_calls",
        ),
        fake_openai.Round(text=("the wiki says yes",)),
    )
    events = await collect(
        CustomRuntime(
            host="openrouter",
            model="anthropic/claude-sonnet-5",
            client_factory=client,
        ),
        [spec("wiki_get")],
        call_tool=_router,
    )

    done = terminal(events)
    assert isinstance(done, ev.AgentCompleted) and done.text == "the wiki says yes"
    assert client.requests[0]["tools"][0]["type"] == "function"
    assert client.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "design/wiki_get says yes",
    }


# -- the model comes off the row --------------------------------------------


def test_a_row_that_declares_a_model_supplies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic publishes one set of model ids and every row on it answers to them, so
    a deployment that names no model still has one — and the runtime reports the value
    in force, which is what a bare `/model` in the shell prints."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    backend = CustomRuntime(host="anthropic")
    assert backend.model == "claude-sonnet-5"
    assert backend.available()
    assert CustomRuntime(host="anthropic", model="claude-opus-5").model == (
        "claude-opus-5"
    )


def test_a_row_that_declares_none_says_so_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat host names deployments whoever stood it up chose, so a default there
    would be right on one machine and a 404 everywhere else."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    backend = CustomRuntime(host="openrouter")
    assert backend.model == ""
    reason = backend.unavailable_reason()
    assert reason is not None and "no model is set" in reason
    assert "runtimes: custom:" in reason


# -- a setting one protocol takes and the other does not ---------------------


def test_tokens_param_on_a_messages_row_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently dropping it would leave the file saying a setting is on while nothing
    honours it — the same rule an unknown option follows."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    reason = CustomRuntime(
        host="anthropic", tokens_param="max_tokens"
    ).unavailable_reason()
    assert reason is not None
    assert "tokens_param" in reason and "anthropic" in reason
    assert "Messages protocol" in reason


@pytest.mark.anyio
async def test_tokens_param_on_a_chat_row_is_sent_under_that_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest lever for a resource whose API version accepts only the older name,
    and the row gets no say in it."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    client = fake_openai.FakeClient(fake_openai.Round(text=("hi",)))
    events = await collect(
        CustomRuntime(
            host="openai",
            model="gpt-5",
            client_factory=client,
            tokens_param="max_tokens",
            max_tokens=4096,
        )
    )
    assert isinstance(terminal(events), ev.AgentCompleted)
    assert client.requests[0]["max_tokens"] == 4096
    assert "max_completion_tokens" not in client.requests[0]
