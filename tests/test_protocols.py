"""The two adapters, at the seam: what one round comes to, and nothing else.

`test_custom_messages.py` and `test_custom_chat.py` drive these through the loop, which
is where the assertions about events, transcripts and tool dispatch belong. What is
here is the classification itself — the `Outcome` a round yields — because that is the
part a second loop will depend on and the part a new stop reason changes.

The same fakes both suites use, for the same reason: the context-manager nesting, the
order of streamed events against the final message, and a call arriving as fragments
are the parts most likely to be wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from latent_intel.agent.protocols import Outcome
from latent_intel.agent.protocols.base import Adapter
from latent_intel.agent.protocols.chat import ChatAdapter, arguments
from latent_intel.agent.protocols.messages import MessagesAdapter
from tests.fixtures import fake_anthropic, fake_openai


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def played(adapter: Adapter, client: Any) -> tuple[list[str], Outcome]:
    """One round, as the text it streamed and the one `Outcome` it ended with."""
    text: list[str] = []
    outcome: Outcome | None = None
    async for item in adapter.round(client, {"model": "m", "messages": []}):
        if isinstance(item, Outcome):
            outcome = item
        else:
            text.append(item)
    assert outcome is not None, "an adapter yields exactly one Outcome, last"
    return text, outcome


# -- the messages adapter ---------------------------------------------------


@pytest.mark.anyio
async def test_messages_reports_an_answer_as_done() -> None:
    adapter = MessagesAdapter()
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(text=("Compaction ", "is bounded."))
    )
    text, outcome = await played(adapter, client)

    assert text == ["Compaction ", "is bounded."]
    assert outcome.status == "done"
    assert outcome.assistant["role"] == "assistant"
    assert outcome.counts["input_tokens"] == 1


@pytest.mark.anyio
async def test_messages_reports_a_paused_turn_as_continue() -> None:
    """`pause_turn` is the API asking for the request again, not a failure."""
    adapter = MessagesAdapter()
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(text=("thinking",), stop_reason="pause_turn")
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "continue"


@pytest.mark.anyio
async def test_messages_carries_the_blocks_and_the_calls_of_a_tool_round() -> None:
    """The content list goes back verbatim — a thinking block returned without its
    signature is rejected on the next request — and the calls come off it decoded."""
    adapter = MessagesAdapter()
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(
            thinking="let me look",
            tools=(("call_1", "design_wiki_get", {"key": "gist"}),),
            stop_reason="tool_use",
        )
    )
    _, outcome = await played(adapter, client)

    assert outcome.status == "tools"
    blocks = outcome.assistant["content"]
    assert [b.type for b in blocks] == ["thinking", "tool_use"]
    assert blocks[0].signature == "signature"
    assert [(c.id, c.name, c.arguments) for c in outcome.calls] == [
        ("call_1", "design_wiki_get", {"key": "gist"})
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop", ["max_tokens", "refusal", "model_context_window_exceeded"]
)
async def test_messages_names_every_stop_reason_it_knows_is_not_an_answer(
    stop: str,
) -> None:
    adapter = MessagesAdapter()
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(text=("half an ans",), stop_reason=stop)
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed"
    assert outcome.kind == stop and outcome.message and outcome.remedy


@pytest.mark.anyio
async def test_messages_reports_a_reason_it_does_not_know_under_its_own_name() -> None:
    """Treating it as `end_turn` would report a truncated answer as a complete one."""
    adapter = MessagesAdapter()
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(text=("...",), stop_reason="something_new")
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed" and outcome.kind == "something_new"
    assert "something_new" in outcome.message


@pytest.mark.anyio
async def test_messages_reports_no_stop_reason_at_all_as_a_cut_stream() -> None:
    adapter = MessagesAdapter()
    client = fake_anthropic.FakeClient(
        fake_anthropic.Round(text=("Compaction is",), stop_reason=None)
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed" and outcome.kind == "unexpected_stop"
    assert "without a stop reason" in outcome.message


# -- the chat adapter -------------------------------------------------------


@pytest.mark.anyio
async def test_chat_reports_an_answer_as_done() -> None:
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(text=("Compaction ", "is bounded."))
    )
    text, outcome = await played(adapter, client)

    assert text == ["Compaction ", "is bounded."]
    assert outcome.status == "done"
    assert outcome.assistant == {
        "role": "assistant",
        "content": "Compaction is bounded.",
    }
    assert outcome.counts["input_tokens"] == 1


@pytest.mark.anyio
async def test_chat_reassembles_a_call_from_its_fragments() -> None:
    """The trap this protocol adds: the arguments arrive as string pieces keyed by
    index, and reading only the first one calls the tool with half an object."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(
            tool_calls=(("call_1", "design_wiki_get", ('{"key"', ': "gi', 'st"}')),),
            finish_reason="tool_calls",
        )
    )
    _, outcome = await played(adapter, client)

    assert outcome.status == "tools"
    assert [(c.id, c.name, c.arguments) for c in outcome.calls] == [
        ("call_1", "design_wiki_get", {"key": "gist"})
    ]
    assert outcome.assistant["tool_calls"][0]["function"]["arguments"] == (
        '{"key": "gist"}'
    )


@pytest.mark.anyio
async def test_chat_leaves_arguments_that_are_not_an_object_undecoded() -> None:
    """None rather than `{}`: the loop dispatches such a call through a router that
    raises, so the model is shown its own mistake rather than a connector being handed
    half an object."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(
            tool_calls=(("call_1", "design_wiki_get", ('{"key": "gi',)),),
            finish_reason="tool_calls",
        )
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "tools"
    assert outcome.calls[0].arguments is None


@pytest.mark.anyio
async def test_chat_synthesises_an_id_where_the_host_sent_none() -> None:
    """Some local servers omit it, and the same string has to appear in the assistant
    message and in the `tool` message that answers it."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(
            tool_calls=((None, "design_wiki_get", ("{}",)),),
            finish_reason="tool_calls",
        )
    )
    _, outcome = await played(adapter, client)
    assert outcome.calls[0].id == "call_0"
    assert outcome.assistant["tool_calls"][0]["id"] == "call_0"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("finish", "kind"),
    [("length", "max_tokens"), ("content_filter", "content_filter")],
)
async def test_chat_maps_a_finish_reason_to_the_kind_both_protocols_share(
    finish: str, kind: str
) -> None:
    """A frontend switching on the kind must not need two vocabularies."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(text=("half an ans",), finish_reason=finish)
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed"
    assert outcome.kind == kind and outcome.message and outcome.remedy


@pytest.mark.anyio
async def test_chat_reads_the_finish_reason_before_it_considers_a_call() -> None:
    """`length` cuts the arguments mid-JSON, and dispatching that fragment reports the
    model's own broken call as a failed tool while the output limit is never said."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(
            tool_calls=(("c1", "design_wiki_get", ('{"ref": "wi',)),),
            finish_reason="length",
        )
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed" and outcome.kind == "max_tokens"
    assert outcome.calls == []


@pytest.mark.anyio
async def test_chat_refuses_a_tool_finish_with_nothing_reassembled() -> None:
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(fake_openai.Round(finish_reason="tool_calls"))
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed" and outcome.kind == "unexpected_stop"
    assert "no calls" in outcome.message


@pytest.mark.anyio
async def test_chat_lets_the_calls_decide_rather_than_the_reason() -> None:
    """A host that streams tool calls and then says `stop` still asked for them."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(
            tool_calls=(("c1", "design_wiki_get", ("{}",)),), finish_reason="stop"
        )
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "tools"


@pytest.mark.anyio
async def test_chat_reports_a_reason_it_does_not_know_as_unexpected() -> None:
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(text=("...",), finish_reason="something_new")
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed" and outcome.kind == "unexpected_stop"
    assert "something_new" in outcome.message


@pytest.mark.anyio
async def test_chat_reports_no_finish_reason_at_all_as_a_cut_stream() -> None:
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(
        fake_openai.Round(text=("Compaction is",), finish_reason=None)
    )
    _, outcome = await played(adapter, client)
    assert outcome.status == "failed" and outcome.kind == "unexpected_stop"
    assert "without a stop reason" in outcome.message


@pytest.mark.anyio
async def test_chat_counts_a_round_the_host_sent_no_usage_for() -> None:
    """Reporting one round for two round-trips understates exactly the questions that
    cost most, so the round is counted with no counts at all."""
    adapter = ChatAdapter()
    client = fake_openai.FakeClient(fake_openai.Round(text=("hi",), usage=None))
    _, outcome = await played(adapter, client)
    assert outcome.status == "done" and outcome.counts == {}


# -- the rule both runtimes on this protocol share ---------------------------


@pytest.mark.parametrize(
    "raw", ["", "{}", '{"key": "gist"}'], ids=["empty", "object", "arguments"]
)
def test_arguments_decodes_every_json_object(raw: str) -> None:
    assert isinstance(arguments(raw), dict)


@pytest.mark.parametrize(
    "raw", ['{"key": "gi', '"a string"', "[1, 2]", "null"], ids=range(4)
)
def test_arguments_refuses_anything_that_is_not_a_json_object(raw: str) -> None:
    """A truncated fragment and a bare string are the same failure, and it is the
    model's to fix. `openai_agents.py` reads the same rule from here."""
    assert arguments(raw) is None
