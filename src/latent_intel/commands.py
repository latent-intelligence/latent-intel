"""What a frontend asks the session to do.

A typed, serializable union rather than a method call, for one reason: the day an HTTP
transport exists, a `Command` goes in and an `AgentEvent` stream comes out, and neither
side needs a new API. A frontend that called `session.find(...)` directly would need
rewriting for that; one that builds `Find(...)` and hands it to `session.run` does not.

Python callers who just want a value still get the typed methods on `Session` — this
union is for the paths that stream.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class Connect(BaseModel):
    type: Literal["connect"] = "connect"
    #: A registered id, or a path/URI. `kind` is inferred from the registry when the
    #: spec names one, and must be given otherwise.
    spec: str
    kind: str | None = None
    source_id: str | None = None
    #: Force the registry's `paths.mirror` — the deployment case, and how you check
    #: that a published mirror is current.
    remote: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class Disconnect(BaseModel):
    type: Literal["disconnect"] = "disconnect"
    source_id: str


class ListSources(BaseModel):
    type: Literal["list_sources"] = "list_sources"


class Find(BaseModel):
    type: Literal["find"] = "find"
    query: str
    #: One source, or every connected source when absent.
    source: str | None = None
    limit: int = 10
    filters: dict[str, Any] = Field(default_factory=dict)


class Fetch(BaseModel):
    type: Literal["fetch"] = "fetch"
    ref: str


class Ask(BaseModel):
    type: Literal["ask"] = "ask"
    prompt: str


Command = Annotated[
    Connect | Disconnect | ListSources | Find | Fetch | Ask,
    Field(discriminator="type"),
]

__all__ = [
    "Ask",
    "Command",
    "Connect",
    "Disconnect",
    "Fetch",
    "Find",
    "ListSources",
]
