"""The event stream — the contract between the core and every frontend.

Write this file first and change it carefully. A CLI, an interactive shell and a web
workbench all consume the same stream; anything that lives here has to survive being
serialized, queued, replayed and rendered by code that does not exist yet.

**Every field is JSON-serializable.** Not "usually" — a single non-serializable field
works in-process for both terminal frontends and is discovered only when the web client
is written, which is exactly too late to fix cheaply.

**Every event carries the same envelope.** `session_id` and `operation_id` give
correlation, `sequence` gives ordering, `parent_id` gives nesting (a tool call under a
turn), `ts` gives timing and `schema_version` gives a consumer the right to refuse. None
of these matter for a synchronous CLI printing lines. All of them matter the moment
events cross a socket, and adding them later means rewriting every producer.

**Unknown types render, they do not raise.** `parse_event` returns `UnknownEvent` for a
`type` it does not recognise, so a newer producer never breaks an older renderer. The
alternative — every renderer changing whenever the vocabulary grows — is the coupling
this whole design exists to avoid.

**The vocabulary ships whole, including the events nothing emits yet.** An unemitted
event class costs a few lines; a vocabulary extended after three frontends exist costs
three frontends.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .models import (
    Approval,
    Artifact,
    AttachedSource,
    Descriptor,
    Doc,
    Fingerprint,
    Hit,
    ToolSpec,
    WebScope,
)

#: Bumped when a change would make an older consumer misread a newer stream. Additive
#: changes — a new event type, a new optional field — do not bump it, because tolerant
#: parsing already covers them.
SCHEMA_VERSION = 1

#: The format a recording file declares in its `RunContext` header, at `format_version`
#: `SCHEMA_VERSION` — one constant, because a change that breaks an older reader of the
#: events breaks an older reader of the file. Spec: `docs/formats/recording.md`.
RECORDING_FORMAT = "latent-intel.recording"

#: What a recording is for, so a share, a demo and an eval run can be told apart from
#: the file alone.
PURPOSES = ("share", "demo", "eval")


def _now() -> datetime:
    return datetime.now(UTC)


class BaseEvent(BaseModel):
    """The envelope every event carries, flat rather than nested.

    Flat because the consumers that need it most are the ones furthest away: `jq` over a
    JSONL replay, a browser reading SSE, a log grep. `event.sequence` beats
    `event.envelope.sequence` in all three, and the nesting buys nothing in return.
    """

    type: str
    schema_version: int = SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    #: One user action. Every event it causes shares this, which is what lets a renderer
    #: group a turn without guessing from timing.
    operation_id: UUID
    #: The event this one happened inside, when it happened inside one.
    parent_id: UUID | None = None
    #: Monotonic within an operation. Ordering survives a transport that does not
    #: preserve it, and replay can assert it.
    sequence: int = 0
    ts: datetime = Field(default_factory=_now)


# -- what the person did ----------------------------------------------------


class UserMessage(BaseEvent):
    type: Literal["user_message"] = "user_message"
    text: str
    #: Skills this message ran with `/name`. Their bodies stay in the prompt for the
    #: rest of the session, which a recording could not show without this.
    skills: list[str] = Field(default_factory=list)


# -- what the model said ----------------------------------------------------


class AssistantToken(BaseEvent):
    """One chunk of streamed output. Chunks, not characters — a renderer appends."""

    type: Literal["assistant_token"] = "assistant_token"
    text: str


class AgentCompleted(BaseEvent):
    type: Literal["agent_completed"] = "agent_completed"
    text: str = ""
    #: True when `text` was already delivered incrementally as `AssistantToken`. A
    #: terminal renderer that printed the tokens must not print it again; a web client
    #: that buffered them uses it to reconcile. Optional and additive, so
    #: `SCHEMA_VERSION` does not move.
    streamed: bool = False
    #: References the answer rests on, so a claim can be traced without re-running it.
    citations: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    #: What the turn cost in US dollars, where the runtime is told — today only
    #: `claude-agent-sdk`, which reports its binary's own estimate. A float, so it
    #: cannot live in `usage`. Optional and additive, so `SCHEMA_VERSION` does not move.
    cost_usd: float | None = None


class AgentFailed(BaseEvent):
    """A failure is an event, not an exception.

    A frontend consuming a stream cannot catch an exception raised inside a generator it
    is iterating over a socket. Every failure a user should see travels as an event.
    """

    type: Literal["agent_failed"] = "agent_failed"
    message: str
    kind: str = "error"
    #: What to try instead, when there is something.
    remedy: str = ""


# -- where context came from ------------------------------------------------


class RetrievalStarted(BaseEvent):
    type: Literal["retrieval_started"] = "retrieval_started"
    query: str
    sources: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseEvent):
    """Hits from **one** source.

    One event per source rather than one merged list, because a lexical score and a
    vector cosine are not comparable and a single ordered list would assert that they
    are. Grouping is structural here so no renderer has to remember it.
    """

    type: Literal["retrieval_result"] = "retrieval_result"
    source_id: str
    query: str
    hits: list[Hit] = Field(default_factory=list)
    truncated: bool = False


class DocumentFetched(BaseEvent):
    type: Literal["document_fetched"] = "document_fetched"
    doc: Doc


# -- what the agent ran -----------------------------------------------------


class ToolStarted(BaseEvent):
    """MCP tools and native tools alike. One pair of events, not one per kind — a
    renderer showing tool activity should not care which side of the seam it came
    from."""

    type: Literal["tool_started"] = "tool_started"
    tool: str
    source_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    effect: str = "external_write"


class ToolResult(BaseEvent):
    type: Literal["tool_result"] = "tool_result"
    tool: str
    source_id: str
    ok: bool = True
    output: str = ""
    error: str = ""
    duration_ms: int | None = None
    #: The `source:key` refs in `output`, in order — filled by the session, so
    #: retrieval can be scored without parsing text. Optional and additive.
    refs: list[str] = Field(default_factory=list)


# -- what is attached -------------------------------------------------------


class SourceConnected(BaseEvent):
    type: Literal["source_connected"] = "source_connected"
    descriptor: Descriptor


class SourceDisconnected(BaseEvent):
    type: Literal["source_disconnected"] = "source_disconnected"
    source_id: str


class ContextAdded(BaseEvent):
    """Material pinned into the working context by hand, not retrieved."""

    type: Literal["context_added"] = "context_added"
    ref: str
    source_id: str
    title: str = ""
    tokens: int | None = None


class ContextRemoved(BaseEvent):
    type: Literal["context_removed"] = "context_removed"
    ref: str
    reason: str = ""


# -- what came out, and what needs a decision -------------------------------


class ArtifactCreated(BaseEvent):
    type: Literal["artifact_created"] = "artifact_created"
    artifact: Artifact


class ApprovalRequested(BaseEvent):
    """Emitted before anything that changes the world. The stream pauses here."""

    type: Literal["approval_requested"] = "approval_requested"
    approval: Approval


class ApprovalResolved(BaseEvent):
    type: Literal["approval_resolved"] = "approval_resolved"
    approval_id: str
    granted: bool
    #: True when a policy decided rather than a person, so a transcript can tell the
    #: difference between consent and configuration.
    automatic: bool = False


# -- what produced the rest -------------------------------------------------


class RunContext(BaseEvent):
    """What produced the events after it: the header of a recording.

    First in every recording, and again whenever any of it changes, so each stretch of
    a file says which runtime, model, project and context engineering made it. Carries
    the format name and version itself, so a file split or concatenated stays readable.
    Written by a recording, not by `Session.run`: the `--json` stream is the wire
    format and has no header.

    Names, never locations — sources are ids and kinds, the project is a name, and
    context engineering is content hashes. `case_id`, `variant_id` and `epoch` are
    filled by batch eval runs.
    """

    type: Literal["run_context"] = "run_context"
    format: str = RECORDING_FORMAT
    format_version: int = SCHEMA_VERSION
    #: A string rather than a Literal, so a purpose added later still parses here as a
    #: header; the schema holds producers to the list.
    purpose: str = Field("share", json_schema_extra={"enum": list(PURPOSES)})
    title: str = ""
    latent_intel_version: str = ""
    runtime: str | None = None
    host: str | None = None
    model: str | None = None
    approval: str = "ask"
    #: How much of the open web the turns after it could reach. None in a header
    #: written before the field existed, which is "not recorded" rather than `off`.
    #: Optional and additive, so `SCHEMA_VERSION` does not move.
    web: WebScope | None = None
    project: str | None = None
    context: Fingerprint = Field(default_factory=Fingerprint)
    sources: list[AttachedSource] = Field(default_factory=list)
    case_id: str | None = None
    variant_id: str | None = None
    epoch: int | None = None


# -- tools offered ----------------------------------------------------------


class ToolsChanged(BaseEvent):
    """The router's namespace after a source connected or dropped."""

    type: Literal["tools_changed"] = "tools_changed"
    tools: list[ToolSpec] = Field(default_factory=list)


# -- the unknown ------------------------------------------------------------


class UnknownEvent(BaseEvent):
    """An event this build does not recognise, preserved rather than dropped.

    Not part of the discriminated union — it is what parsing falls back to. A renderer
    shows it as a generic line; a proxy can forward it untouched. Either way a newer
    producer does not break an older consumer.
    """

    type: str = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)


AgentEvent = Annotated[
    UserMessage
    | AssistantToken
    | AgentCompleted
    | AgentFailed
    | RetrievalStarted
    | RetrievalResult
    | DocumentFetched
    | ToolStarted
    | ToolResult
    | SourceConnected
    | SourceDisconnected
    | ContextAdded
    | ContextRemoved
    | ArtifactCreated
    | ApprovalRequested
    | ApprovalResolved
    | RunContext
    | ToolsChanged,
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)

#: Every type this build knows. `intel doctor` prints it; a test asserts the union and
#: this set agree, so a new class cannot be added to one and forgotten in the other.
KNOWN_TYPES: frozenset[str] = frozenset(
    member.model_fields["type"].default
    for member in AgentEvent.__origin__.__args__  # type: ignore[attr-defined]
)


def parse_event(data: dict[str, Any] | str) -> AgentEvent | UnknownEvent:
    """Read one event, tolerantly.

    An unrecognised `type`, a missing field, a `schema_version` from the future — all of
    them produce an `UnknownEvent` carrying the original payload rather than an
    exception. A stream is a thing you are in the middle of; failing hard partway
    through one loses everything already received, to guard against something a
    renderer could have shown as a grey line.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return UnknownEvent(
                type="unparseable",
                session_id=uuid4(),
                operation_id=uuid4(),
                payload={"raw": data},
            )
    if not isinstance(data, dict):
        return UnknownEvent(
            type="unparseable",
            session_id=uuid4(),
            operation_id=uuid4(),
            payload={"raw": repr(data)},
        )
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError:
        return UnknownEvent(
            type=str(data.get("type") or "unknown"),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            session_id=_uuid_or_new(data.get("session_id")),
            operation_id=_uuid_or_new(data.get("operation_id")),
            sequence=int(data.get("sequence") or 0),
            payload=data,
        )


def _uuid_or_new(value: Any) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return uuid4()


def recording_schema() -> dict[str, Any]:
    """The JSON Schema every line of a recording satisfies, as committed at
    `schemas/recording.v1.json`. Per line, because the file is JSON Lines; that the
    first line is a `run_context` is stated in the spec and checked in tests."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{RECORDING_FORMAT}.v{SCHEMA_VERSION}",
        "title": f"{RECORDING_FORMAT} v{SCHEMA_VERSION}, one line",
        "description": (
            "One event of a recording. A recording opens with a run_context; a reader "
            "skips a type it does not know and refuses a missing or unknown "
            "format_version. Spec: docs/formats/recording.md."
        ),
        **_ADAPTER.json_schema(mode="serialization"),
    }


def dump_event(event: BaseEvent) -> str:
    """One event as a single JSON line — the replay and SSE wire format."""
    return event.model_dump_json()


def read_stream(text: str) -> Iterator[AgentEvent | UnknownEvent]:
    """Parse a JSONL replay. Blank lines are skipped; bad lines become UnknownEvent."""
    for line in text.splitlines():
        if line.strip():
            yield parse_event(line)


class Emitter:
    """Stamps envelopes so producers never assemble one by hand.

    A producer that builds its own envelope will eventually forget `sequence`, or reuse
    an `operation_id` across two operations, and the bug surfaces as events rendering in
    the wrong order under load — the kind of thing that is very hard to see and trivial
    to prevent.
    """

    def __init__(
        self,
        session_id: UUID,
        operation_id: UUID | None = None,
        parent_id: UUID | None = None,
    ) -> None:
        self.session_id = session_id
        self.operation_id = operation_id or uuid4()
        self.parent_id = parent_id
        self._sequence = 0

    def emit(self, event_class: type[Any], **fields: Any) -> Any:
        """Build one event, envelope filled in."""
        self._sequence += 1
        return event_class(
            session_id=self.session_id,
            operation_id=self.operation_id,
            parent_id=self.parent_id,
            sequence=self._sequence,
            **fields,
        )

    def nested(self, parent: BaseEvent) -> Emitter:
        """An emitter for work happening inside `parent` — a tool call within a turn.

        Shares the operation but continues this emitter's sequence, so a flat consumer
        that ignores nesting still sees one correctly ordered stream.
        """
        child = Emitter(self.session_id, self.operation_id, parent.event_id)
        child._sequence = self._sequence
        return child


__all__ = [
    "SCHEMA_VERSION",
    "AgentCompleted",
    "AgentEvent",
    "AgentFailed",
    "ApprovalRequested",
    "ApprovalResolved",
    "ArtifactCreated",
    "AssistantToken",
    "BaseEvent",
    "ContextAdded",
    "ContextRemoved",
    "DocumentFetched",
    "Emitter",
    "KNOWN_TYPES",
    "PURPOSES",
    "RECORDING_FORMAT",
    "RetrievalResult",
    "RetrievalStarted",
    "RunContext",
    "SourceConnected",
    "SourceDisconnected",
    "ToolResult",
    "ToolStarted",
    "ToolsChanged",
    "UnknownEvent",
    "UserMessage",
    "dump_event",
    "parse_event",
    "read_stream",
    "recording_schema",
]
