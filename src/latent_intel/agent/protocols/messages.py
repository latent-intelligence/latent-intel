"""The Anthropic Messages protocol: transcript, stream and stop-reason vocabulary.

Lifted from `runtimes/anthropic.py`'s turn on 2026-09-17, when the loop around it moved
to `runtimes/custom.py`. What is here is what that module had which its OpenAI sibling
did not; everything the two shared went to `agent/turn.py` or the loop.

**Assistant content goes back verbatim.** A thinking block returned without its
signature is rejected on the next request, so the content list is appended as it
arrived rather than rebuilt from the text we happened to read off the stream.

**An empty system prompt and an empty tool list are omitted, not sent.** The API
rejects an empty tool list outright, and an empty system prompt is a wasted
instruction.

**A stop reason this build does not know is not a success.** It is reported under its
own name: treating it as `end_turn` would report a truncated answer as a complete one,
and a vocabulary we have not read is exactly the case where that is likely.

**Hosted web tools run inside the round.** The host searches and fetches itself and
returns `server_tool_use` blocks paired with their results, so there is nothing to
dispatch — only something to report. Usually the pair arrives in one final message; a
`pause_turn` can split it across two, so pairing is the turn's (`HostedCalls`), not the
round's. An error comes back as a result carrying an error code, never as an exception,
and is reported as a failed call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from ... import events as ev
from ...models import Message, ToolSpec, WebScope
from .. import turn
from .base import Call, Outcome, Served

#: Stop reasons that end a turn without an answer, each with what to do about it. A
#: reason absent from here and not in the completing set is reported under its own name
#: rather than guessed at — a vocabulary this build does not know is not a success.
STOP_FAILURES: dict[str, tuple[str, str]] = {
    "max_tokens": (
        "the model reached its output limit before finishing",
        "raise `max_tokens` under this runtime, or ask a narrower question",
    ),
    "refusal": (
        "the model declined to answer",
        "rephrase the question",
    ),
    "model_context_window_exceeded": (
        "the conversation no longer fits the model's context window",
        "start a new session, or attach fewer sources",
    ),
}

#: Stop reasons that mean the model finished saying what it had to say.
STOP_DONE = frozenset({"end_turn", "stop_sequence"})

#: The hosted tools a web scope offers, by the name the model calls them under. Any
#: other server tool — the code a dynamic filter runs, say — is the host's business.
WEB_TOOLS = ("web_search", "web_fetch")

#: The result blocks the two come back in, each paired to its call by `tool_use_id`.
_RESULT_TYPES = ("web_search_tool_result", "web_fetch_tool_result")

#: The per-request counts `usage.server_tool_use` carries, under the names it uses.
SERVER_USAGE_KEYS = ("web_search_requests", "web_fetch_requests")


def web_definitions(scope: WebScope, versions: tuple[str, str]) -> list[dict[str, Any]]:
    """The hosted tools `scope` offers, at the versions the row declares: search, and
    under `browse` fetch as well. Empty under `off`.

    The domain list and `max_uses` go on each tool, because that is where the API takes
    them; a request carrying both lists is refused upstream, and `WebScope` refuses it
    first. Fetch is asked for citations, which every published version takes and none
    turns on by default: without them an answer resting on a page it read cites nothing.
    """
    if scope.mode == "off":
        return []
    common: dict[str, Any] = {}
    if scope.allowed_domains:
        common["allowed_domains"] = list(scope.allowed_domains)
    if scope.blocked_domains:
        common["blocked_domains"] = list(scope.blocked_domains)
    if scope.max_uses is not None:
        common["max_uses"] = scope.max_uses
    search, fetch = versions
    tools = [{"type": search, "name": "web_search", **common}]
    if scope.mode == "browse":
        tools.append(
            {
                "type": fetch,
                "name": "web_fetch",
                **common,
                "citations": {"enabled": True},
            }
        )
    return tools


class HostedCalls:
    """The hosted web calls of one turn, each held until its result comes back.

    One per turn, read once per round. A call whose result has not arrived is held
    rather than reported: a `pause_turn` can end a round between the two, and the
    result opens the next. Only what is still held when the turn ends is reported as
    failed — a recording that lost a search would claim an answer rested on less of the
    web than it did.

    Also the turn's list of fetched pages, in the order they arrived, which is what a
    citation of a fetched page points into: it carries the page's position, not its URL.
    """

    def __init__(self) -> None:
        self._waiting: dict[str, Served] = {}
        self._results: dict[str, Any] = {}
        #: `(url, title)` per page fetched this turn, in order.
        self._pages: list[tuple[str, str]] = []
        self._cited: dict[str, None] = {}

    def read(self, content: Sequence[Any], emitter: ev.Emitter) -> list[ev.AgentEvent]:
        """The pairs for the calls `content` completes, in the order they were made —
        with the citations its text makes recorded for `citations`."""
        for block in content:
            kind = getattr(block, "type", "")
            if kind == "server_tool_use" and str(block.name) in WEB_TOOLS:
                self._waiting[str(block.id)] = Served(
                    name=str(block.name), arguments=dict(block.input or {})
                )
            elif kind in _RESULT_TYPES:
                self._results[str(block.tool_use_id)] = block
                page = block.content
                if kind == "web_fetch_tool_result" and getattr(page, "url", None):
                    self._pages.append((str(page.url), _document_title(page)))
        events: list[ev.AgentEvent] = []
        for identifier in list(self._waiting):
            result = self._results.pop(identifier, None)
            if result is None:
                continue
            call = self._waiting.pop(identifier)
            _read(call, result.content)
            events += turn.served(call, emitter)
        for ref in self._citing(content):
            self._cited[ref] = None
        return events

    def close(self, emitter: ev.Emitter) -> list[ev.AgentEvent]:
        """The pairs for what is still held as the turn ends, each failed. Called
        before the turn's terminal event, on every path to one."""
        events: list[ev.AgentEvent] = []
        for call in self._waiting.values():
            call.ok, call.error = False, "no result came back for this call"
            events += turn.served(call, emitter)
        self._waiting.clear()
        return events

    @property
    def citations(self) -> list[str]:
        """`web:<url>` for each page the turn's text cites, in order, once each."""
        return list(self._cited)

    def _citing(self, content: Sequence[Any]) -> list[str]:
        """The refs one message's text cites: a search result by its URL, a fetched
        page by its position among the turn's fetched pages.

        A position that names no page, or a page whose title disagrees with the one the
        citation carries, is dropped rather than guessed at: a citation pointing at the
        wrong page is worse than one left out.
        """
        found: list[str] = []
        for block in content:
            if getattr(block, "type", "") != "text":
                continue
            for citation in getattr(block, "citations", None) or ():
                if url := getattr(citation, "url", None):
                    found.append(f"web:{url}")
                    continue
                index = getattr(citation, "document_index", None)
                if not isinstance(index, int) or not 0 <= index < len(self._pages):
                    continue
                url, title = self._pages[index]
                claimed = getattr(citation, "document_title", None)
                if claimed and title and claimed != title:
                    continue
                found.append(f"web:{url}")
        return found


def _read(call: Served, content: Any) -> None:
    """One result's content into `call`: a list of search hits, one fetched page, or
    an error object naming its code."""
    code = getattr(content, "error_code", None)
    if code is not None:
        call.ok, call.error = False, f"{call.name.replace('_', ' ')} failed: {code}"
        return
    pages = content if isinstance(content, list) else [content]
    lines: list[str] = []
    for page in pages:
        url = str(getattr(page, "url", "") or "")
        if not url:
            continue
        title = str(getattr(page, "title", "") or _document_title(page))
        lines.append(f"{title} — {url}" if title else url)
        call.refs.append(f"web:{url}")
    call.refs = list(dict.fromkeys(call.refs))
    call.output = "\n".join(lines)


def _document_title(page: Any) -> str:
    """A fetched page's title, which sits on the document it carries. The page's text
    is left out: it went to the model, and a recording is not a crawl."""
    return str(getattr(getattr(page, "content", None), "title", "") or "")


def usage_counts(usage: Any) -> dict[str, int | None]:
    """One round's usage under `turn.USAGE_KEYS` and `SERVER_USAGE_KEYS` names — the
    tokens, and the hosted calls the round was billed for."""
    counts: dict[str, int | None] = {
        key: getattr(usage, key, None) for key in turn.USAGE_KEYS
    }
    server = getattr(usage, "server_tool_use", None)
    counts.update({key: getattr(server, key, None) for key in SERVER_USAGE_KEYS})
    return counts


class MessagesAdapter:
    """The Messages protocol, behind the `Adapter` Protocol. Stateless: one instance
    serves every turn, and nothing about a round survives the `Outcome` it returns."""

    def definitions(
        self, wired: Sequence[tuple[str, ToolSpec]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": spec.description,
                "input_schema": turn.input_schema(spec),
            }
            for name, spec in wired
        ]

    def transcript(
        self, messages: Sequence[Message], system: str
    ) -> list[dict[str, Any]]:
        """Text only. A prior turn's tool blocks are not replayed: they refer to
        `tool_use_id`s from a request this one never made.

        An empty message is dropped rather than sent: the API rejects a non-final
        assistant message with empty content, so one turn that completed with no text
        would fail every later question in the session. The system prompt is a request
        parameter on this protocol, so it is not a message here.
        """
        return [
            {"role": message.role, "content": message.text}
            for message in messages
            if message.text.strip()
        ]

    def request(
        self,
        *,
        model: str,
        max_tokens: int,
        tokens_param: str | None,
        transcript: list[dict[str, Any]],
        definitions: list[dict[str, Any]],
        system: str,
    ) -> dict[str, Any]:
        """`tokens_param` is not read: this protocol has one name for the output limit,
        and a runtime that was given one for a host speaking it says so rather than
        dropping it here."""
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": transcript,
        }
        # Omitted rather than sent empty: the API rejects an empty tool list, and an
        # empty system prompt is a wasted instruction.
        if system:
            request["system"] = system
        if definitions:
            request["tools"] = definitions
        return request

    async def round(
        self, client: Any, request: dict[str, Any]
    ) -> AsyncIterator[str | Outcome]:
        async with client.messages.stream(**request) as rounds:
            async for event in rounds:
                if event.type != "text":
                    continue
                yield str(event.text)
            final = await rounds.get_final_message()
        yield self._outcome(final)

    def _outcome(self, final: Any) -> Outcome:
        """The final message, classified, with its blocks for `HostedCalls` to read.

        Carried whatever the stop reason: a round that ended on a refusal still
        searched, and a recording that dropped the search would misstate what was read.
        """
        outcome = self._classify(final)
        outcome.blocks = list(final.content)
        return outcome

    def _classify(self, final: Any) -> Outcome:
        """The final message, classified."""
        # Verbatim, thinking blocks included: a thinking block returned without its
        # signature is rejected on the next request.
        assistant = {"role": "assistant", "content": final.content}
        counts = usage_counts(final.usage)
        stop = final.stop_reason

        if stop == "tool_use":
            return Outcome(
                status="tools",
                assistant=assistant,
                counts=counts,
                calls=[
                    Call(
                        id=str(block.id),
                        name=str(block.name),
                        arguments=dict(block.input or {}),
                    )
                    for block in final.content
                    if getattr(block, "type", "") == "tool_use"
                ],
            )
        if stop == "pause_turn":
            # A long-running turn the API asks us to resume.
            return Outcome(status="continue", assistant=assistant, counts=counts)
        if stop in STOP_DONE:
            return Outcome(status="done", assistant=assistant, counts=counts)
        # No stop reason at all is a stream that was cut — by a proxy, by a host that
        # ended the response early — and calling it success reports whatever arrived
        # before the cut as the whole answer.
        if stop is None:
            return Outcome(
                status="failed",
                assistant=assistant,
                counts=counts,
                kind="unexpected_stop",
                message="the stream ended without a stop reason",
                remedy="ask again",
            )
        message, remedy = STOP_FAILURES.get(
            str(stop), (f"the model stopped: {stop}", "")
        )
        return Outcome(
            status="failed",
            assistant=assistant,
            counts=counts,
            kind=str(stop),
            message=message,
            remedy=remedy,
        )

    def results(
        self, results: Sequence[tuple[Call, ev.ToolResult]]
    ) -> list[dict[str, Any]]:
        """Every call's result in one user message, which is what this protocol wants:
        a round that answers only some of them is rejected on the next request."""
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        # The model is told what failed; a failed call it cannot see is
                        # one it repeats.
                        "content": result.error or result.output,
                        "is_error": not result.ok,
                    }
                    for call, result in results
                ],
            }
        ]


__all__ = [
    "SERVER_USAGE_KEYS",
    "STOP_DONE",
    "STOP_FAILURES",
    "WEB_TOOLS",
    "HostedCalls",
    "MessagesAdapter",
    "usage_counts",
    "web_definitions",
]
