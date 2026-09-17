"""The seam itself: what a loop asks a protocol for, and what it gets back.

**A round is text, then exactly one `Outcome`.** The adapter reads the stream and says
what the round came to; the loop decides what to do about it. That split is what lets
the vocabulary of stop reasons grow inside one adapter without a second one — or the
loop — being touched.

**An adapter never raises for a stop reason.** A refusal, an output limit, a stream cut
short: each is an `Outcome` with `status="failed"` and the sentence to report. What an
SDK raises is left alone and travels up to the runtime's failure ladder, which is the
only place that knows which SDK's exception classes to read.

**An adapter never emits an event.** It returns values, the way a connector does, so a
sequence number and an envelope stay out of reach — and so a change to the event
vocabulary touches no adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ... import events as ev
from ...models import Message, ToolSpec


@dataclass
class Call:
    """One tool call the model asked for, in the vocabulary dispatch already speaks."""

    #: What ties the result back to the call. Synthesised by an adapter whose host
    #: omitted one, so the same string is used in both messages either way.
    id: str
    #: The name as it went out on the wire. Not invertible — see `turn.wire_name` — so
    #: the runtime looks it up in the lookup it built rather than splitting it apart.
    name: str
    #: The decoded arguments, or None where what arrived was not a JSON object. None is
    #: the model's mistake to fix and never reaches a connector: the runtime dispatches
    #: it through a router that raises, so the failure comes back as a result the model
    #: is shown rather than as a dead turn.
    arguments: dict[str, Any] | None


@dataclass
class Outcome:
    """What one round came to. Exactly one per round, and the last thing yielded."""

    #: `done` — the model finished; `continue` — the API asked for the request again;
    #: `tools` — run `calls` and go round again; `failed` — end the turn, saying why.
    status: Literal["done", "continue", "tools", "failed"]
    #: What to append to the transcript, verbatim. Built by the adapter because only it
    #: knows the shape its next request will be rejected for getting wrong.
    assistant: dict[str, Any]
    #: The calls to run, when `status` is `tools`.
    calls: list[Call] = field(default_factory=list)
    #: This round's usage, under `turn.USAGE_KEYS` names, so a frontend totalling cost
    #: reads one vocabulary whichever protocol answered. A count the host did not send
    #: is absent or None and is skipped rather than counted as zero.
    counts: dict[str, int | None] = field(default_factory=dict)
    #: The `AgentFailed` fields, when `status` is `failed`.
    kind: str = ""
    message: str = ""
    remedy: str = ""


class Adapter(Protocol):
    """One endpoint shape, as the five questions a loop has to ask something."""

    def definitions(
        self, wired: Sequence[tuple[str, ToolSpec]]
    ) -> list[dict[str, Any]]:
        """The offered tools, as this protocol spells a tool definition."""
        ...

    def transcript(
        self, messages: Sequence[Message], system: str
    ) -> list[dict[str, Any]]:
        """The history, as this protocol's messages — including where the system prompt
        goes, which is a message on one protocol and a parameter on the other."""
        ...

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
        """One request's kwargs. `tokens_param` is the name the output limit goes out
        under where the protocol has more than one; the protocol that has one ignores
        it."""
        ...

    def round(
        self, client: Any, request: dict[str, Any]
    ) -> AsyncIterator[str | Outcome]:
        """One model round-trip: the text deltas as they arrive, then one `Outcome`.

        Declared `def` rather than `async def` for the reason `agent/base.py` gives
        about `stream`: a Protocol member written `async def ... -> AsyncIterator[T]`
        types as a coroutine returning one, which no implementation satisfies under
        `mypy --strict`.
        """
        ...

    def results(
        self, results: Sequence[tuple[Call, ev.ToolResult]]
    ) -> list[dict[str, Any]]:
        """What the model is sent back after the calls ran — one message on one
        protocol, one per call on the other."""
        ...


__all__ = ["Adapter", "Call", "Outcome"]
