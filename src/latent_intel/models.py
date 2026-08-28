"""The value types that travel between a connector, the session and a frontend.

Everything here is a Pydantic model with JSON-serializable fields, for the same reason
the events are: a web client has to receive these over the wire unchanged, and a type
that only works in-process would be discovered too late to fix cheaply.

Two things carry a design decision rather than just data:

**`Provenance` is required on every result.** A hit or a document that cannot say where
it came from is worse than no result, because a reader has no signal to doubt it. The
reference architecture states this as a rule for MCP tools; here it is a required field,
which is the version that cannot be forgotten.

**`Effect` is declared, never inferred.** Guessing whether a tool writes — from its
name, its description, or an absent annotation — puts a guess between an agent and a
filesystem. A tool that declares nothing is treated as `external_write`, the safe end,
and reported as under-declared rather than quietly trusted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Effect(StrEnum):
    """What a tool does to the world. Declared by the tool, never guessed."""

    NONE = "none"
    EXTERNAL_READ = "external_read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"

    @property
    def writes(self) -> bool:
        return self in (self.LOCAL_WRITE, self.EXTERNAL_WRITE, self.DESTRUCTIVE)


class Capability(StrEnum):
    """What a connector can do. Not every source does everything.

    An MCP server may expose only tools; a directory of markdown has no tools; a wiki
    has all three. A single fat Protocol would force empty implementations and let a
    connector lie about what it supports.
    """

    SEARCH = "search"
    FETCH = "fetch"
    TOOLS = "tools"


class Ref(BaseModel):
    """`source:key` — the same colon rule `latent-wiki` uses, so a reference means the
    same thing in both programs."""

    source_id: str
    key: str

    def __str__(self) -> str:
        return f"{self.source_id}:{self.key}"

    @classmethod
    def parse(cls, text: str, *, default_source: str | None = None) -> Ref:
        """Split `source:key`. A bare key resolves against the current source.

        A `scheme://` URL is not a ref — it has no source prefix, and reading one as
        though it did would silently address the wrong store.
        """
        if "://" not in text:
            source, sep, key = text.partition(":")
            if sep and source and key:
                return cls(source_id=source, key=key)
        if not default_source:
            raise ValueError(
                f"{text!r} has no source prefix and no source is selected — "
                f"write 'source:key' or pick one with /use"
            )
        return cls(source_id=default_source, key=text)


class Provenance(BaseModel):
    """Where a result came from. Required on every result-bearing event."""

    source_id: str
    #: What produced it — `search`, `fetch`, or a tool name.
    method: str
    #: The reference within the source, when there is one.
    ref: str | None = None
    #: The underlying material a curated result derives from.
    origin: str | None = None
    #: When the material was true, as the source states it.
    as_of: str | None = None


class Descriptor(BaseModel):
    """What a connected source is, cheaply. The scan rung for sources themselves."""

    id: str
    kind: str
    title: str = ""
    capabilities: list[Capability] = Field(default_factory=list)
    #: Pages, files, rows, tools — whatever the source counts in.
    count: int | None = None
    unit: str = ""
    #: When the source was last built or refreshed, if it says.
    freshness: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities


class Hit(BaseModel):
    """One search result, at the gist rung. A lead, never a body."""

    ref: str
    source_id: str
    title: str
    kind: str = ""
    lead: str = ""
    #: Comparable *within* a source and nowhere else — a lexical score and a vector
    #: cosine are different quantities. The session groups by source rather than
    #: merging, so nothing ever implies a comparison this number cannot support.
    score: float = 0.0
    provenance: Provenance


class Doc(BaseModel):
    """One document in full — the detail rung."""

    ref: str
    source_id: str
    title: str
    body: str
    kind: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance


class ToolSpec(BaseModel):
    """A tool a connector contributes to the router."""

    name: str
    source_id: str
    description: str = ""
    #: Absent declarations are `EXTERNAL_WRITE` at construction, not at call time — see
    #: `from_mcp`. Deciding late means the unsafe default is one forgotten branch away.
    effect: Effect = Effect.EXTERNAL_WRITE
    #: True when the source did not declare an effect and the safe default was applied.
    #: `intel doctor` reports these; they are not errors, they are unknowns.
    effect_declared: bool = True
    input_schema: dict[str, Any] = Field(default_factory=dict)

    @property
    def qualified(self) -> str:
        """`source.tool` — the router's namespace, so two sources may both offer
        `search` without colliding."""
        return f"{self.source_id}.{self.name}"


class Artifact(BaseModel):
    """Something produced during a session that outlives the message it appeared in."""

    id: str
    kind: str
    title: str = ""
    path: str | None = None
    preview: str = ""


class Approval(BaseModel):
    """A request to run something that changes the world, and what was decided."""

    id: str
    tool: str
    effect: Effect
    summary: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class SourceRequest(BaseModel):
    """One source to attach: what `Session.connect` takes, as a value.

    Exists so `connect_many` has something to iterate that is not a tuple of five
    positional arguments, and so a caller in `frontends/` can build one without
    importing the connector package.
    """

    spec: str
    kind: str | None = None
    source_id: str | None = None
    remote: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One turn of conversation, as a runtime receives it.

    JSON-serializable like everything else here, so a transport carrying a `Command` in
    and events out needs no new type.
    """

    role: Literal["user", "assistant"] = "user"
    text: str


class SessionError(Exception):
    """Anything the session refuses to do, phrased for a person."""


class ConnectError(SessionError):
    """A source could not be connected. Carries what to try instead where it helps."""


class CapabilityError(SessionError):
    """A source was asked for something it does not do."""


class RuntimeUnavailable(SessionError):
    """No agent runtime is configured, or the configured one is not installed."""


class RuntimeFailed(SessionError):
    """A runtime was reachable and then failed — a non-zero exit, an unreadable stream,
    an API error.

    Distinct from `RuntimeUnavailable` because the remedy differs: one says configure
    something, the other says look at what broke.
    """


__all__ = [
    "Approval",
    "Artifact",
    "Capability",
    "CapabilityError",
    "ConnectError",
    "Descriptor",
    "Doc",
    "Effect",
    "Hit",
    "Message",
    "Provenance",
    "Ref",
    "RuntimeFailed",
    "RuntimeUnavailable",
    "SessionError",
    "SourceRequest",
    "ToolSpec",
]
