"""A scripted Anthropic client, shaped exactly like the one the SDK returns.

The alternative was mocking `messages.stream` per test, which would have left the parts
most likely to be wrong — the context-manager nesting, the order of streamed events
against `get_final_message`, the transcript the next round is sent — untested.

One `Round` per model round-trip. The client records every request it was given, so a
test can assert what was *sent* as well as what came back: a tool result that never
reaches the second request is a bug no assertion about events would catch.

**One client, two loops.** `messages.stream` is the path `protocols/messages.py` drives
itself; `beta.messages.tool_runner` returns a `_FakeRunner` driving the same scripted
rounds the way `BaseAsyncToolRunner.__run__` does — including the part that matters most
to `runtimes/anthropic_sdk.py`, which is that **the runner calls our tools**, between
yielding one round's stream and opening the next request. Faking that with a runner that
never called `tool.call` would leave the bridge, the relay and the ordering they produce
entirely untested.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from anthropic.lib.tools import (
    BetaAsyncBuiltinFunctionTool,
    BetaAsyncFunctionTool,
    ToolError,
)


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
    #: `ServerToolUsage` — `web_search_requests`, `web_fetch_requests` — or None.
    server_tool_use: Any = None


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
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=1, output_tokens=1))
    thinking: str = ""
    #: Raised when the request is made, the way the SDK raises on a bad response.
    raises: BaseException | None = None
    #: The final message's content exactly, in place of the blocks built from `text`
    #: and `tools` — for hosted tool blocks, which arrive interleaved with the text.
    content: tuple[Any, ...] | None = None


class FakeClient:
    """The client, the factory that builds it, and the record of what it was sent."""

    def __init__(self, *rounds: Round) -> None:
        self.rounds = list(rounds)
        self.requests: list[dict[str, Any]] = []
        self.messages = _Messages(self)
        self.beta = _Beta(self)
        #: `(name, input)` per tool the *runner* invoked. The runtime under test never
        #: appends here, so this is the assertion that execution went through the SDK.
        self.calls: list[tuple[str, Any]] = []
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


class _Beta:
    def __init__(self, client: FakeClient) -> None:
        self.messages = _BetaMessages(client)


class _BetaMessages:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def tool_runner(self, **request: Any) -> _FakeRunner:
        return _FakeRunner(self._client, request)


class _FakeRunner:
    """`BetaAsyncStreamingToolRunner`, over scripted rounds.

    The shape it copies, from `anthropic/lib/tools/_beta_runner.py`: the tool
    definitions are `to_dict()`ed once at construction and carried in the params every
    request is made from; each iteration yields the round's stream, and *after control
    returns* reads the final message, runs any tool calls and appends both the
    assistant message and the results to the transcript. A `ToolError` becomes a
    `tool_result` with `is_error`, any other exception becomes its `repr` with the
    same flag, and a name that is not among the tools never reaches one. A definition
    that is not a runnable tool — a hosted tool's dict — is sent and never dispatched,
    and only `tool_use` blocks are run, as the real runner does;
    `tests/test_anthropic_sdk_runtime.py` holds that runner to the same.
    """

    def __init__(self, client: FakeClient, request: dict[str, Any]) -> None:
        self._client = client
        runnable = (BetaAsyncFunctionTool, BetaAsyncBuiltinFunctionTool)
        self._tools = [t for t in request["tools"] if isinstance(t, runnable)]
        raw = [t for t in request["tools"] if not isinstance(t, runnable)]
        self._max_iterations = request.get("max_iterations")
        self._params: dict[str, Any] = {
            key: value
            for key, value in request.items()
            if key not in ("tools", "stream", "max_iterations")
        }
        self._params["messages"] = list(request["messages"])
        self._params["tools"] = [*(tool.to_dict() for tool in self._tools), *raw]

    def set_messages_params(
        self, params: Callable[[dict[str, Any]], dict[str, Any]] | dict[str, Any]
    ) -> None:
        """The public seam the runtime uses to drop `tools:` when it has none."""
        self._params = params(self._params) if callable(params) else params

    async def __aiter__(self) -> AsyncIterator[_Stream]:
        iterations = 0
        while self._max_iterations is None or iterations < self._max_iterations:
            index = len(self._client.requests)
            # Recorded at call time, like `_StreamManager`: the transcript is one list
            # the runner appends to, so recording it by reference would show every
            # round the state of the last one.
            self._client.requests.append(
                {
                    **self._params,
                    "messages": list(self._params["messages"]),
                    "max_iterations": self._max_iterations,
                }
            )
            assert index < len(self._client.rounds), (
                "the fake ran out of scripted rounds"
            )
            scripted = self._client.rounds[index]
            if scripted.raises is not None:
                raise scripted.raises

            stream = _Stream(scripted)
            yield stream
            iterations += 1

            final = await stream.get_final_message()
            if final.stop_reason in ("pause_turn", "compaction"):
                self._append({"role": "assistant", "content": final.content})
                continue
            if final.stop_reason != "tool_use":
                return
            results = await self._run_tools(final)
            if results is None:
                return
            self._append({"role": "assistant", "content": final.content}, results)

    def _append(self, *messages: dict[str, Any]) -> None:
        self._params["messages"] = [*self._params["messages"], *messages]

    async def _run_tools(self, final: FinalMessage) -> dict[str, Any] | None:
        blocks = [b for b in final.content if getattr(b, "type", "") == "tool_use"]
        if not blocks:
            return None
        by_name = {tool.name: tool for tool in self._tools}
        results: list[dict[str, Any]] = []
        for block in blocks:
            tool = by_name.get(block.name)
            if tool is None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error: Tool '{block.name}' not found",
                        "is_error": True,
                    }
                )
                continue
            self._client.calls.append((block.name, block.input))
            try:
                content: Any = await tool.call(block.input)
            except ToolError as exc:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": exc.content,
                        "is_error": True,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — the runner reports, never raises
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": repr(exc),
                        "is_error": True,
                    }
                )
            else:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    }
                )
        return {"role": "user", "content": results}


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
        if self._round.content is not None:
            return FinalMessage(
                content=list(self._round.content),
                stop_reason=self._round.stop_reason,
                usage=self._round.usage,
            )
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


# -- hosted web blocks --------------------------------------------------------
#
# The SDK's own types, not lookalikes: a hosted call arrives as a `server_tool_use`
# beside a result block carrying either the pages or an error object, and a fake that
# guessed the shape would test the guess. Shared by both loops on this protocol.


def search(identifier: str, query: str) -> Any:
    from anthropic.types import ServerToolUseBlock

    return ServerToolUseBlock(
        id=identifier, name="web_search", input={"query": query}, type="server_tool_use"
    )


def hits(identifier: str, *pages: tuple[str, str]) -> Any:
    from anthropic.types import WebSearchResultBlock, WebSearchToolResultBlock

    return WebSearchToolResultBlock(
        tool_use_id=identifier,
        type="web_search_tool_result",
        content=[
            WebSearchResultBlock(
                encrypted_content="opaque",
                title=title,
                url=url,
                type="web_search_result",
            )
            for title, url in pages
        ],
    )


def fetch(identifier: str, url: str) -> Any:
    from anthropic.types import ServerToolUseBlock

    return ServerToolUseBlock(
        id=identifier, name="web_fetch", input={"url": url}, type="server_tool_use"
    )


def page(identifier: str, url: str, title: str) -> Any:
    from anthropic.types import (
        DocumentBlock,
        PlainTextSource,
        WebFetchBlock,
        WebFetchToolResultBlock,
    )

    return WebFetchToolResultBlock(
        tool_use_id=identifier,
        type="web_fetch_tool_result",
        content=WebFetchBlock(
            url=url,
            type="web_fetch_result",
            content=DocumentBlock(
                type="document",
                title=title,
                source=PlainTextSource(
                    data="the page text", media_type="text/plain", type="text"
                ),
            ),
        ),
    )


def cited(text: str, *urls: str) -> Any:
    from anthropic.types import CitationsWebSearchResultLocation, TextBlock

    return TextBlock(
        type="text",
        text=text,
        citations=[
            CitationsWebSearchResultLocation(
                cited_text="…",
                encrypted_index="opaque",
                url=url,
                type="web_search_result_location",
            )
            for url in urls
        ],
    )


def cited_page(text: str, index: int, title: str | None) -> Any:
    """Text citing a fetched page, as the host cites one: by its position among the
    documents the turn fetched, with no URL on the citation."""
    from anthropic.types import CitationCharLocation, TextBlock

    return TextBlock(
        type="text",
        text=text,
        citations=[
            CitationCharLocation(
                cited_text="…",
                document_index=index,
                document_title=title,
                start_char_index=0,
                end_char_index=1,
                type="char_location",
            )
        ],
    )
