"""A scripted OpenAI client, shaped exactly like the one the SDK returns.

The alternative was mocking `chat.completions.create` per test, which would have left
the parts most likely to be wrong — a tool call arriving as fragments across several
chunks, usage arriving on a chunk with no choice at all, the transcript the next round
is sent — untested.

One `Round` per model round-trip, and one chunk per streamed piece: a call's id and
name arrive on one chunk and its arguments on later ones, because that is how the wire
delivers them and reassembling them is this runtime's job.

The client records every request it was given, so a test can assert what was *sent* as
well as what came back: a tool result that never reaches the second request is a bug no
assertion about events would catch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any


@dataclass
class FunctionFragment:
    name: str | None = None
    arguments: str | None = None


@dataclass
class ToolCallFragment:
    """One piece of one call. `index` is what ties the pieces together — the id and the
    name arrive once, the arguments in as many fragments as the model felt like."""

    index: int
    id: str | None = None
    function: FunctionFragment | None = None
    type: str = "function"


@dataclass
class Delta:
    content: str | None = None
    tool_calls: list[ToolCallFragment] | None = None


@dataclass
class Choice:
    delta: Delta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class PromptTokensDetails:
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_tokens_details: PromptTokensDetails | None = None


@dataclass
class Chunk:
    choices: list[Choice] = field(default_factory=list)
    usage: Usage | None = None


@dataclass
class Round:
    """One round-trip: what streams, how it finishes, or what it raises."""

    text: tuple[str, ...] = ()
    #: `(id, name, argument fragments)` per call, in the order they stream.
    tool_calls: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    finish_reason: str | None = "stop"
    usage: Usage | None = field(
        default_factory=lambda: Usage(prompt_tokens=1, completion_tokens=1)
    )
    #: Raised when the request is made, the way the SDK raises on a bad response.
    raises: BaseException | None = None


def _chunks(scripted: Round) -> list[Chunk]:
    """The round, as the chunk sequence the wire would deliver."""
    chunks = [
        Chunk(choices=[Choice(delta=Delta(content=piece))])
        for piece in scripted.text
    ]
    for index, (identifier, name, fragments) in enumerate(scripted.tool_calls):
        chunks.append(
            Chunk(
                choices=[
                    Choice(
                        delta=Delta(
                            tool_calls=[
                                ToolCallFragment(
                                    index=index,
                                    id=identifier,
                                    function=FunctionFragment(name=name),
                                )
                            ]
                        )
                    )
                ]
            )
        )
        for fragment in fragments:
            chunks.append(
                Chunk(
                    choices=[
                        Choice(
                            delta=Delta(
                                tool_calls=[
                                    ToolCallFragment(
                                        index=index,
                                        function=FunctionFragment(arguments=fragment),
                                    )
                                ]
                            )
                        )
                    ]
                )
            )
    chunks.append(
        Chunk(choices=[Choice(delta=Delta(), finish_reason=scripted.finish_reason)])
    )
    if scripted.usage is not None:
        # No choice at all on this one: the shape a runtime indexing `choices[0]`
        # unconditionally would crash on.
        chunks.append(Chunk(usage=scripted.usage))
    return chunks


class FakeClient:
    """The client, the factory that builds it, and the record of what it was sent."""

    def __init__(self, *rounds: Round) -> None:
        self.rounds = list(rounds)
        self.requests: list[dict[str, Any]] = []
        self.chat = _Chat(self)
        self.closed = False

    def __call__(self, **_: Any) -> FakeClient:
        """Usable directly as `client_factory`, which is called with no arguments, and
        as a stand-in for the constructor, which is called with several."""
        return self

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> bool:
        self.closed = True
        return False


class _Chat:
    def __init__(self, client: FakeClient) -> None:
        self.completions = _Completions(client)


class _Completions:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def create(self, **request: Any) -> AsyncIterator[Chunk]:
        #: The transcript is one list the runtime appends to, so recording it by
        #: reference would show every round the state of the last one. The SDK
        #: serializes at call time; so does this.
        self._client.requests.append(
            {**request, "messages": [dict(m) for m in request["messages"]]}
        )
        index = len(self._client.requests) - 1
        assert index < len(self._client.rounds), "the fake ran out of scripted rounds"
        scripted = self._client.rounds[index]
        if scripted.raises is not None:
            raise scripted.raises
        return _stream(scripted)


async def _stream(scripted: Round) -> AsyncIterator[Chunk]:
    for chunk in _chunks(scripted):
        yield chunk
