"""A scripted Anthropic client, shaped exactly like the one the SDK returns.

The alternative was mocking `messages.stream` per test, which would have left the parts
most likely to be wrong — the context-manager nesting, the order of streamed events
against `get_final_message`, the transcript the next round is sent — untested.

One `Round` per model round-trip. The client records every request it was given, so a
test can assert what was *sent* as well as what came back: a tool result that never
reaches the second request is a bug no assertion about events would catch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any


@dataclass
class TextEvent:
    """What the SDK yields while a message streams. Only `text` matters to us."""

    text: str
    type: str = "text"


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ThinkingBlock:
    """Current models return one of these first. It is appended to the transcript
    verbatim, signature and all, or the next request is rejected."""

    thinking: str
    signature: str = "signature"
    type: str = "thinking"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass
class FinalMessage:
    content: list[Any]
    stop_reason: str | None
    usage: Usage


@dataclass
class Round:
    """One round-trip: what streams, what the final message says, or what it raises."""

    text: tuple[str, ...] = ()
    #: `(id, name, arguments)` per tool_use block.
    tools: tuple[tuple[str, str, dict[str, Any]], ...] = ()
    stop_reason: str | None = "end_turn"
    usage: Usage = field(
        default_factory=lambda: Usage(input_tokens=1, output_tokens=1)
    )
    thinking: str = ""
    #: Raised when the request is made, the way the SDK raises on a bad response.
    raises: BaseException | None = None


class FakeClient:
    """The client, the factory that builds it, and the record of what it was sent."""

    def __init__(self, *rounds: Round) -> None:
        self.rounds = list(rounds)
        self.requests: list[dict[str, Any]] = []
        self.messages = _Messages(self)
        self.closed = False

    def __call__(self) -> FakeClient:
        """Usable directly as `client_factory` — the SDK constructor is called the
        same way, with no arguments."""
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


class _Messages:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def stream(self, **request: Any) -> _StreamManager:
        return _StreamManager(self._client, request)


class _StreamManager:
    def __init__(self, client: FakeClient, request: dict[str, Any]) -> None:
        self._client = client
        #: The transcript is one list the runtime appends to, so recording it by
        #: reference would show every round the state of the last one. The SDK
        #: serializes at call time; so does this.
        self._request = {**request, "messages": list(request["messages"])}

    async def __aenter__(self) -> _Stream:
        self._client.requests.append(self._request)
        index = len(self._client.requests) - 1
        assert index < len(self._client.rounds), "the fake ran out of scripted rounds"
        scripted = self._client.rounds[index]
        if scripted.raises is not None:
            raise scripted.raises
        return _Stream(scripted)

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> bool:
        return False


class _Stream:
    def __init__(self, scripted: Round) -> None:
        self._round = scripted

    def __aiter__(self) -> AsyncIterator[TextEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[TextEvent]:
        for chunk in self._round.text:
            yield TextEvent(text=chunk)

    async def get_final_message(self) -> FinalMessage:
        content: list[Any] = []
        if self._round.thinking:
            content.append(ThinkingBlock(thinking=self._round.thinking))
        if self._round.text:
            content.append(TextBlock(text="".join(self._round.text)))
        for identifier, name, arguments in self._round.tools:
            content.append(ToolUseBlock(id=identifier, name=name, input=arguments))
        return FinalMessage(
            content=content,
            stop_reason=self._round.stop_reason,
            usage=self._round.usage,
        )
