"""The runtime seam — where an answer comes from.

The second of the two extension points, and the mirror of `connectors/base.py`. That
parallel is deliberate: a runtime is discovered, built and reported exactly the way a
connector is, so someone who has read one file already knows this one.

**A runtime owns how much of the loop it wants.** `api` and `openrouter` will hand the
tool list to our own router and drive the turn themselves. `claude-cli` shells to a
binary with its own agent loop, reaching our sources as MCP servers instead. Both
asymmetries live behind `stream`; what comes out is the same event stream either way.

**`stream` is declared `def`, not `async def`, and that is not a slip.** An `async def`
whose body yields has type `Callable[..., AsyncIterator[T]]`, but a Protocol member
*written* `async def ... -> AsyncIterator[T]` types as
`Callable[..., Coroutine[Any, Any, AsyncIterator[T]]]`. Under `mypy --strict` those do
not match, and no implementation would satisfy the Protocol.

**A runtime never stamps an envelope.** The session builds the `Emitter` and passes it
in, so sequence numbers and operation ids stay out of a runtime author's hands — the
same bargain connectors get by returning values instead of events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from importlib.metadata import entry_points
from typing import Any, Protocol, cast, runtime_checkable

from .. import events as ev
from ..models import Message, RuntimeUnavailable, ToolSpec

#: The entry-point group a third party publishes into. Our own runtimes use it too, so
#: the extension path is the one we take ourselves and cannot rot unnoticed.
ENTRY_POINT_GROUP = "latent_intel.runtimes"


@runtime_checkable
class Runtime(Protocol):
    """One way of getting a model to answer, behind one event stream."""

    id: str

    def available(self) -> bool:
        """Whether this backend can actually run here — a binary on PATH, a key in the
        environment.

        Cheap and synchronous: `intel doctor` calls it for every installed runtime, and
        a probe that made a network request would make diagnosis slower than the thing
        being diagnosed.
        """
        ...

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        """One turn, as events. See the module docstring for why this is not `async`."""
        ...


def available_kinds() -> dict[str, type]:
    """Every runtime class registered under the entry-point group.

    Loaded on demand and failures swallowed, so a runtime whose module is not written
    yet — or whose optional dependency is missing — is simply absent from the list
    rather than an ImportError at start-up. `pyproject.toml` registers `api` and
    `openrouter` against modules that do not exist; this is what lets that stay true
    without anyone deleting the declaration.
    """
    kinds: dict[str, type] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            kinds[entry.name] = entry.load()
        except Exception:  # noqa: BLE001 — a broken plugin must not stop the program
            continue
    return kinds


def build(kind: str, **options: Any) -> Runtime:
    """Instantiate one runtime by kind.

    Unlike `connectors.build` there is no id or target to pass: a runtime is not
    attached to anything, so forcing the parallel would mean two ignored arguments.
    """
    kinds = available_kinds()
    if kind not in kinds:
        known = ", ".join(sorted(kinds)) or "none installed"
        raise RuntimeUnavailable(f"no runtime of kind '{kind}' (installed: {known})")
    return cast(Runtime, kinds[kind](**options))


__all__ = ["ENTRY_POINT_GROUP", "Runtime", "available_kinds", "build"]
