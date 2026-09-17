"""The OpenAI-compatible protocol: transcript, chunk reassembly and finish reasons.

Lifted from `runtimes/openai.py`'s turn on 2026-09-17, when the loop around it moved to
`runtimes/custom.py`. What is here is what that module had which its Anthropic sibling
did not; everything the two shared went to `agent/turn.py` or the loop.

**The system prompt is a message, and it is first.** A later one is advice the model
has already been talking over.

**Call ids are synthesised where a host omits one.** Some local servers stream a call
with no id at all. The id is only ever a key tying the `tool` message back to the
assistant's call, so one is made from the fragment's index and used in both places
rather than sending `""` twice and hoping the host matches them.

**The finish reason is read before any call is considered.** A call truncated by the
output limit arrives as unparseable JSON, and dispatching it would report the model's
own broken fragment as a failed tool and burn the round bound retrying it, while the
real reason — the output limit — was never said at all. After that the calls decide,
not the reason: a host that streams tool calls and then says `stop` still asked for
them.

**`stream_options` is sent on every request.** Without it the final chunk carries no
usage at all, and a tool-heavy turn would report nothing about what it cost.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from ... import events as ev
from ...models import Message, ToolSpec
from .. import turn
from .base import Call, Outcome

#: Finish reasons that end a turn without an answer, each with the kind it is reported
#: under and what to do about it. The wire names are this protocol's; the kinds match
#: `messages.py`, so a frontend switching on one does not need two vocabularies.
STOP_FAILURES: dict[str, tuple[str, str, str]] = {
    "length": (
        "max_tokens",
        "the model reached its output limit before finishing",
        "raise `max_tokens` under this runtime, or ask a narrower question",
    ),
    "content_filter": (
        "content_filter",
        "the endpoint's content filter stopped the response",
        "rephrase the question",
    ),
}

#: The one finish reason that means the model finished saying what it had to say. A
#: reason absent from here and from `STOP_FAILURES` is reported rather than guessed at:
#: treating it as success would report a truncated answer as a complete one.
STOP_DONE = frozenset({"stop"})

#: The output-token parameter a host that declares none is sent. `max_tokens` is
#: deprecated on the OpenAI API and rejected by reasoning models; a row that wants the
#: older name says so, and a runtime overrides both.
DEFAULT_TOKENS_PARAM = "max_completion_tokens"


def arguments(raw: str) -> dict[str, Any] | None:
    """One call's arguments, decoded, or None where what arrived was not a JSON object.

    A truncated fragment and a bare string are the same failure, and it is the model's
    to fix. Public because `openai_agents.py` needs the same rule: the protocol is what
    decides that arguments arrive as a JSON string, so the two runtimes that speak it
    must agree about a string that is not one, or a fragment routed by one and refused
    by the other is a difference nobody chose.
    """
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


class ChatAdapter:
    """The OpenAI-compatible protocol, behind the `Adapter` Protocol. Stateless: one
    instance serves every turn, and nothing about a round survives the `Outcome`."""

    def definitions(
        self, wired: Sequence[tuple[str, ToolSpec]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description,
                    "parameters": turn.input_schema(spec),
                },
            }
            for name, spec in wired
        ]

    def transcript(
        self, messages: Sequence[Message], system: str
    ) -> list[dict[str, Any]]:
        """The system prompt first, then text only. A prior turn's tool messages are
        not replayed: they refer to `tool_call_id`s from a request this one never made.

        An empty message is dropped rather than sent: a turn that completed with no
        text records an empty assistant message, and a host that rejects one would fail
        every later question in the session over a turn that already ended.
        """
        transcript: list[dict[str, Any]] = []
        if system:
            transcript.append({"role": "system", "content": system})
        transcript += [
            {"role": message.role, "content": message.text}
            for message in messages
            if message.text.strip()
        ]
        return transcript

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
        """`system` is not read: on this protocol the prompt is already the transcript's
        first message. `tokens_param` is whatever the caller resolved — the row's name
        or the runtime's override — so the adapter stays row-agnostic."""
        request: dict[str, Any] = {
            "model": model,
            "messages": transcript,
            "stream": True,
            "stream_options": {"include_usage": True},
            tokens_param or DEFAULT_TOKENS_PARAM: max_tokens,
        }
        # Omitted rather than sent empty: the API rejects an empty tool list.
        if definitions:
            request["tools"] = definitions
        return request

    async def round(
        self, client: Any, request: dict[str, Any]
    ) -> AsyncIterator[str | Outcome]:
        said: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        counts: dict[str, int | None] = {}
        finish: str | None = None

        chunks = await client.chat.completions.create(**request)
        async for chunk in chunks:
            if getattr(chunk, "usage", None) is not None:
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                # The same four counters `messages.py` reports, so a frontend totalling
                # cost reads one vocabulary.
                counts = {
                    "input_tokens": chunk.usage.prompt_tokens,
                    "output_tokens": chunk.usage.completion_tokens,
                    "cache_read_input_tokens": getattr(details, "cached_tokens", None),
                    "cache_creation_input_tokens": getattr(
                        details, "cache_write_tokens", None
                    ),
                }
            # The usage chunk carries no choice, and a host may send others that do not
            # either.
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            piece = getattr(delta, "content", None)
            if piece:
                said.append(piece)
                yield str(piece)
            # A call arrives in pieces keyed by `index`: the id and name once, the
            # arguments as string fragments to concatenate in order.
            for fragment in getattr(delta, "tool_calls", None) or []:
                call = calls.setdefault(
                    fragment.index, {"id": "", "name": "", "arguments": ""}
                )
                if getattr(fragment, "id", None):
                    call["id"] = fragment.id
                function = getattr(fragment, "function", None)
                if function is None:
                    continue
                if getattr(function, "name", None):
                    call["name"] = function.name
                if getattr(function, "arguments", None):
                    call["arguments"] += function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        # Some local servers stream a call with no id at all — see the module
        # docstring. Done before the assistant message is built, so both places use it.
        for index in sorted(calls):
            if not calls[index]["id"]:
                calls[index]["id"] = f"call_{index}"
        yield self._outcome(said, calls, counts, finish)

    def _outcome(
        self,
        said: list[str],
        calls: dict[int, dict[str, str]],
        counts: dict[str, int | None],
        finish: str | None,
    ) -> Outcome:
        """The round, classified. Counted as a round either way, even where the host
        sent no usage at all."""
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(said) or None,
        }
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": calls[index]["id"],
                    "type": "function",
                    "function": {
                        "name": calls[index]["name"],
                        "arguments": calls[index]["arguments"],
                    },
                }
                for index in sorted(calls)
            ]

        # Before any dispatch: see the module docstring.
        if finish is not None and finish in STOP_FAILURES:
            kind, message, remedy = STOP_FAILURES[finish]
            return Outcome(
                status="failed",
                assistant=assistant,
                counts=counts,
                kind=kind,
                message=message,
                remedy=remedy,
            )
        # A finish reason asking for tools with nothing reassembled is not a turn to
        # continue: the next round would send an assistant message with neither content
        # nor calls and get the same answer again.
        if finish == "tool_calls" and not calls:
            return Outcome(
                status="failed",
                assistant=assistant,
                counts=counts,
                kind="unexpected_stop",
                message="the model asked for tools but sent no calls",
                remedy="ask again",
            )
        if calls:
            return Outcome(
                status="tools",
                assistant=assistant,
                counts=counts,
                calls=[
                    Call(
                        id=calls[index]["id"],
                        name=calls[index]["name"],
                        arguments=arguments(calls[index]["arguments"]),
                    )
                    for index in sorted(calls)
                ],
            )
        if finish in STOP_DONE:
            return Outcome(status="done", assistant=assistant, counts=counts)
        # No finish reason at all is a stream that was cut — by a proxy, by a host that
        # ended the response early — and calling it success reports whatever arrived
        # before the cut as the whole answer.
        if finish is None:
            return Outcome(
                status="failed",
                assistant=assistant,
                counts=counts,
                kind="unexpected_stop",
                message="the stream ended without a stop reason",
                remedy="ask again",
            )
        return Outcome(
            status="failed",
            assistant=assistant,
            counts=counts,
            kind="unexpected_stop",
            message=f"the model stopped: {finish}",
            remedy="",
        )

    def results(
        self, results: Sequence[tuple[Call, ev.ToolResult]]
    ) -> list[dict[str, Any]]:
        """One `tool` message per call, each carrying its own id: a round that answers
        only the last of them is rejected on the next request."""
        return [
            {
                "role": "tool",
                "tool_call_id": call.id,
                # The model is told what failed; a failed call it cannot see is one it
                # repeats.
                "content": result.error or result.output,
            }
            for call, result in results
        ]


__all__ = [
    "DEFAULT_TOKENS_PARAM",
    "STOP_DONE",
    "STOP_FAILURES",
    "ChatAdapter",
    "arguments",
]
