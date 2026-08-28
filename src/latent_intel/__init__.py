"""latent-intel — a client over heterogeneous context.

Wikis, vector indexes, plain directories and MCP servers, reachable together. The public
surface is deliberately small: a `Session`, the event vocabulary, and the value types
that travel between them. Everything else — connectors, the tool router, the agent
runtime — is reached through `Session` rather than imported.

That narrowness is the design. A CLI, an interactive shell and a later web workbench all
consume the same `AsyncIterator[AgentEvent]`, so a second frontend is an addition rather
than a second implementation of the product.
"""

from __future__ import annotations

from .events import AgentEvent, dump_event, parse_event, read_stream
from .models import (
    Capability,
    Descriptor,
    Doc,
    Effect,
    Hit,
    Provenance,
    Ref,
    SessionError,
    ToolSpec,
)

__all__ = [
    "AgentEvent",
    "Capability",
    "Descriptor",
    "Doc",
    "Effect",
    "Hit",
    "Provenance",
    "Ref",
    "SessionError",
    "ToolSpec",
    "dump_event",
    "parse_event",
    "read_stream",
]

__version__ = "0.1.0"
