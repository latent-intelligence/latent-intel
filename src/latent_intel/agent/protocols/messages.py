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
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from ... import events as ev
from ...models import Message, ToolSpec
from .. import turn
from .base import Call, Outcome

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
        """The final message, classified."""
        # Verbatim, thinking blocks included: a thinking block returned without its
        # signature is rejected on the next request.
        assistant = {"role": "assistant", "content": final.content}
        counts: dict[str, int | None] = {
            key: getattr(final.usage, key, None) for key in turn.USAGE_KEYS
        }
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


__all__ = ["STOP_DONE", "STOP_FAILURES", "MessagesAdapter"]
