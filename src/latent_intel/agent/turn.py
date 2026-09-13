"""The vendor-neutral half of an in-process turn.

`claude-cli` shells to a binary that owns its own agent loop. A runtime talking to an
API owns ours, and the parts of that loop which are *not* about one vendor's wire format
are here: which tools may be offered, what the model is told it is looking at, how one
tool call becomes a paired `ToolStarted`/`ToolResult`, and how usage adds up across
rounds. Only the transcript and stream shapes are vendor-specific, and those stay in the
vendor's own module.

This exists now because there is one caller, not because there will be several. What
justifies it anyway is the boundary it draws: a second vendor's module should be a
transcript translation and nothing else, and anything it copies out of `anthropic.py`
is the signal that the loop itself wants extracting — which is a decision for that day,
not this one.

**Nothing here stamps an envelope.** `dispatch` is handed the `Emitter` the session
built, exactly as a runtime is, so a sequence number stays out of reach here too.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .. import events as ev
from ..models import Effect, ToolSpec

#: What the router is, as a type. A `Session.call_tool` bound method satisfies it, and
#: so does a test's lambda — a Protocol here would buy nothing over the signature.
ToolRouter = Callable[[str, str, dict[str, Any]], Awaitable[str]]

#: A tool name on the wire may carry only these characters. The router's own namespace
#: is `source_id.name`, and the dot is not among them.
_UNSAFE = re.compile(r"[^0-9A-Za-z_-]")

#: And it may not be longer than this. Truncation is what the lookup built by the caller
#: makes survivable: the wire name is not invertible, so nothing tries to reverse it.
_MAX_NAME = 64

#: Usage fields worth carrying. `AgentCompleted.usage` is `dict[str, int]`, and
#: everything else on a usage object is a nested structure pydantic would reject.
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def wire_name(spec: ToolSpec) -> str:
    """The name one tool is offered under, folded to what a wire format accepts.

    Not invertible, for the same reason `claude_cli.sanitise` is not: two source ids can
    fold to one. A caller keeps `{wire_name(spec): spec}` and looks the call back up,
    rather than trying to split the name apart again.
    """
    return _UNSAFE.sub("_", spec.qualified)[:_MAX_NAME]


def offered(tools: Sequence[ToolSpec], approval: str) -> list[ToolSpec]:
    """Which tools the model is allowed to see this turn.

    The same gate `claude_cli.allowed_tools` applies, minus the MCP-reachability filter:
    an in-process runtime reaches every connected source, so there is nothing to
    exclude for being unservable. Under `ask` or `never` only non-writing tools are
    offered, and `ask` therefore means the cautious end until this package grows a real
    approval channel — withholding rather than prompting, because there is no one to
    prompt inside a stream.
    """
    return [
        spec for spec in tools if approval == "auto" or not Effect(spec.effect).writes
    ]


class ToolNameCollision(ValueError):
    """Two attached tools fold to the same wire name.

    Raised rather than resolved: the definitions list would carry a duplicate name,
    which the API rejects with a 400, and the lookup a runtime keeps would silently
    route every call to whichever spec was folded last. Neither failure names the two
    sources that collided, and this one does.
    """


def wired(
    tools: Sequence[ToolSpec], approval: str
) -> list[tuple[str, ToolSpec]]:
    """The tools this turn offers, each paired with the name it goes out under.

    The one place `offered` and `wire_name` are composed, so both runtimes get the
    collision check rather than one of them growing it later. `wire_name` is not
    invertible — a dot, a space and any other unsafe character all fold to `_` — so
    `my wiki`/`search` and `my_wiki`/`search` arrive here as one name.
    """
    pairs: list[tuple[str, ToolSpec]] = []
    taken: dict[str, ToolSpec] = {}
    for spec in offered(tools, approval):
        name = wire_name(spec)
        first = taken.get(name)
        if first is not None:
            raise ToolNameCollision(
                f"'{first.qualified}' and '{spec.qualified}' are both offered to the "
                f"model as '{name}' — attach one of them under a different id"
            )
        taken[name] = spec
        pairs.append((name, spec))
    return pairs


def input_schema(spec: ToolSpec) -> dict[str, Any]:
    """A tool's parameters, as a schema the API will accept.

    `ToolSpec.input_schema` defaults to `{}`, which is a legal Python default and an
    illegal tool definition — the API requires an object schema. A tool that declares
    nothing takes nothing.
    """
    return spec.input_schema or {"type": "object", "properties": {}}


def system_prompt(sources: Sequence[Any]) -> str:
    """Tell the agent what it is looking at, and how to cite it.

    Cheap, and the difference between a generic chat and one that knows it has a wiki
    attached. Citations are recoverable later only if it is asked for them now.
    """
    if not sources:
        return ""
    lines = [
        "You are answering over attached context sources, reachable as tools.",
        "Attached:",
    ]
    for descriptor in sources:
        caps = ", ".join(sorted(str(c) for c in descriptor.capabilities))
        lines.append(f"  {descriptor.id} ({descriptor.kind}) — {caps}")
    lines.append(
        "Cite what you use as `source:key`. Say when the sources do not answer."
    )
    return "\n".join(lines)


def status_message(exc: Any) -> str:
    """A status error from either SDK, as the one sentence a turn fails with.

    The status alone sends someone to a network tab to find out what was wrong with the
    request; the body usually already says — `Unrecognized request argument supplied:
    max_completion_tokens` names the parameter and the fix. The message only: the
    headers carry the credential and the request carries the transcript, and neither
    belongs in an event a frontend renders or a user pastes into a thread.

    Both SDKs shape the exception the same way — a `status_code` and a decoded `body` —
    and a body that is anything else is not an error here, only a report without the
    detail.
    """
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    detail = error.get("message") if isinstance(error, dict) else None
    if isinstance(detail, str) and detail.strip():
        return f"the endpoint returned {exc.status_code}: {detail}"
    return f"the endpoint returned {exc.status_code}"


async def dispatch(
    call_tool: ToolRouter | None,
    spec: ToolSpec | None,
    *,
    source_id: str,
    name: str,
    arguments: dict[str, Any],
    emitter: ev.Emitter,
) -> tuple[ev.ToolStarted, ev.ToolResult]:
    """One tool call, as the pair of events a renderer already knows how to draw.

    Never raises. A tool the model invented, a session that handed over no router, and a
    connector that threw are all the same thing from here — a result with `ok=False` and
    the reason in `error`. A runtime cannot let one bad call end a turn, because the
    model has to be told what happened in order to do anything else.

    The result is emitted on `emitter.nested(started)`, so its `parent_id` is the
    start's `event_id` and a renderer can nest the two without guessing from timing.
    """
    started = emitter.emit(
        ev.ToolStarted,
        tool=name,
        source_id=source_id,
        arguments=arguments,
        effect=str(spec.effect) if spec else str(Effect.EXTERNAL_WRITE),
    )
    child = emitter.nested(started)
    began = time.monotonic()

    output, error = "", ""
    if spec is None:
        error = f"no tool '{name}' is attached"
    elif call_tool is None:
        error = "this runtime was given no tool router"
    else:
        try:
            output = await call_tool(source_id, name, arguments)
        except Exception as exc:  # noqa: BLE001 — a failed call is a result, not a crash
            error = str(exc) or type(exc).__name__

    result = child.emit(
        ev.ToolResult,
        tool=name,
        source_id=source_id,
        ok=not error,
        output=output,
        error=error,
        duration_ms=int((time.monotonic() - began) * 1000),
    )
    return started, result


class UsageTotals:
    """Usage added up across the rounds of one turn.

    A turn that calls tools is several API round-trips, and reporting only the last
    one's usage understates a tool-heavy question by most of its cost. `num_turns`
    counts the rounds, which is what `claude-cli` reports under that name too.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, int] = {}
        self.rounds = 0

    def add(self, usage: Any) -> None:
        """One round, from an SDK object whose fields are already named `USAGE_KEYS`."""
        self.add_counts(**{key: getattr(usage, key, None) for key in USAGE_KEYS})

    def add_counts(self, **counts: int | None) -> None:
        """One round, from named integers — what a protocol naming its usage fields
        differently has after translating them.

        A count that is absent or None is skipped rather than counted as zero: cache
        fields are None when caching was not in play, and a zero would claim it was.
        """
        self.rounds += 1
        for key, value in counts.items():
            if isinstance(value, int):
                self._tokens[key] = self._tokens.get(key, 0) + value

    def totals(self, duration_ms: int) -> dict[str, int]:
        """`AgentCompleted.usage` is `dict[str, int]` — integers only, flat."""
        return {**self._tokens, "duration_ms": duration_ms, "num_turns": self.rounds}


__all__ = [
    "USAGE_KEYS",
    "ToolNameCollision",
    "ToolRouter",
    "UsageTotals",
    "dispatch",
    "input_schema",
    "offered",
    "status_message",
    "system_prompt",
    "wire_name",
    "wired",
]
