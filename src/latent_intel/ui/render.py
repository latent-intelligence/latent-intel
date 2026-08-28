"""Events in, terminal out. The only module that decides how anything looks.

Both terminal frontends share this, and a web client will replace it wholesale while
consuming the identical events — which is why it takes an `AgentEvent` and never a
`Session`, a connector or a store. If this file ever needs to reach back for something,
the event vocabulary is missing a field.

**Unknown events render as a dim line.** A renderer that raised on an unfamiliar `type`
would make every future event addition a breaking change for every frontend at once.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .. import events as ev

#: Long enough to judge relevance, short enough that ten hits still fit on a screen.
LEAD_WIDTH = 160


def render(console: Console, event: ev.AgentEvent | ev.UnknownEvent) -> None:
    """Print one event. Every branch is a `type`, so adding one is adding a branch."""
    match event:
        case ev.SourceConnected():
            _source(console, event)
        case ev.SourceDisconnected():
            console.print(f"  [dim]dropped[/] [source]{escape(event.source_id)}[/]")
        case ev.RetrievalStarted():
            names = ", ".join(event.sources) or "nothing attached"
            console.print(f"[dim]searching {escape(names)}…[/]")
        case ev.RetrievalResult():
            _hits(console, event)
        case ev.DocumentFetched():
            _document(console, event)
        case ev.ToolStarted():
            console.print(
                f"  [tool]▸[/] [tool]{escape(event.tool)}[/] "
                f"[dim]{escape(_arguments(event.arguments))}[/]"
            )
        case ev.ToolResult():
            mark = "[ok]✓[/]" if event.ok else "[fail]✗[/]"
            detail = event.output if event.ok else event.error
            console.print(f"    {mark} [dim]{escape(_clip(detail, 100))}[/]")
        case ev.AssistantToken():
            console.print(escape(event.text), end="", highlight=False)
        case ev.AgentCompleted():
            _completed(console, event)
        case ev.AgentFailed():
            _failed(console, event)
        case ev.ApprovalRequested():
            _approval(console, event)
        case ev.ApprovalResolved():
            verdict = "granted" if event.granted else "refused"
            how = " [dim](by policy)[/]" if event.automatic else ""
            console.print(f"  [dim]approval {verdict}[/]{how}")
        case ev.ContextAdded():
            size = f" [dim]({event.tokens} tokens)[/]" if event.tokens else ""
            console.print(f"  [dim]+ context[/] [ref]{escape(event.ref)}[/]{size}")
        case ev.ContextRemoved():
            console.print(f"  [dim]- context[/] [ref]{escape(event.ref)}[/]")
        case ev.ArtifactCreated():
            where = (
                f" [dim]{escape(event.artifact.path)}[/]" if event.artifact.path else ""
            )
            console.print(f"  [accent]created[/] {escape(event.artifact.title)}{where}")
        case ev.UserMessage() | ev.ToolsChanged():
            pass  # echoed by the frontend that caused it; not worth reprinting
        case _:
            # Including UnknownEvent. A newer producer must never break this renderer.
            console.print(f"  [dim]· {escape(str(event.type))}[/]")


def _source(console: Console, event: ev.SourceConnected) -> None:
    d = event.descriptor
    size = f"{d.count} {d.unit}" if d.count is not None else ""
    caps = " ".join(str(c) for c in d.capabilities)
    fresh = f"  [dim]built {escape(d.freshness)}[/]" if d.freshness else ""
    console.print(
        f"  [ok]✓[/] [dim]{d.kind:<6}[/] [source]{escape(d.id)}[/]  "
        f"[text]{size}[/]  [dim]{caps}[/]{fresh}"
    )


def _hits(console: Console, event: ev.RetrievalResult) -> None:
    """One source's results.

    Grouped under a source heading rather than merged into a global list, because the
    scores are not comparable across sources — a lexical count and a cosine are
    different quantities. The heading is the visible half of that decision.
    """
    console.print()
    console.print(f"[source]{escape(event.source_id)}[/]", end="")
    if not event.hits:
        console.print("  [dim]no matches[/]")
        return
    more = " [dim]+ more[/]" if event.truncated else ""
    console.print(f"  [dim]{len(event.hits)} hit(s)[/]{more}")

    for hit in event.hits:
        kind = f"[kind]{escape(hit.kind)}[/] " if hit.kind else ""
        console.print(
            f"  {kind}[ref]{escape(hit.ref)}[/]  [score]{hit.score:g}[/]",
            highlight=False,
        )
        if hit.lead:
            console.print(f"    [dim]{escape(_clip(hit.lead, LEAD_WIDTH))}[/]")


def _document(console: Console, event: ev.DocumentFetched) -> None:
    doc = event.doc
    console.print()
    console.print(f"[accent.strong]{escape(doc.title)}[/]")
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", no_wrap=True)
    meta.add_column(style="text")
    meta.add_row("ref", doc.ref)
    if why := str(doc.metadata.get("why") or ""):
        meta.add_row("why", why)
    if doc.provenance.origin:
        meta.add_row("origin", doc.provenance.origin)
    if doc.provenance.as_of:
        meta.add_row("as of", doc.provenance.as_of)
    console.print(meta)
    console.print()
    console.print(escape(doc.body))


def _completed(console: Console, event: ev.AgentCompleted) -> None:
    # `streamed` means the tokens already reached the terminal one chunk at a time;
    # printing `text` as well would show the whole answer twice. The field carries the
    # full answer regardless, because a transport consumer needs it.
    if event.text and not event.streamed:
        console.print()
        console.print(escape(event.text))
    if event.citations:
        refs = "  ".join(f"[ref]{escape(c)}[/]" for c in event.citations)
        console.print(f"\n[dim]sources:[/] {refs}")


def _failed(console: Console, event: ev.AgentFailed) -> None:
    console.print(f"[fail]✗[/] {escape(event.message)}")
    if event.remedy:
        console.print(f"  [dim]{escape(event.remedy)}[/]")


def _approval(console: Console, event: ev.ApprovalRequested) -> None:
    a = event.approval
    console.print(
        f"[warn]?[/] [tool]{escape(a.tool)}[/] [dim]({escape(str(a.effect))})[/]"
    )
    console.print(f"  {escape(a.summary)}")


def _arguments(arguments: dict[str, object]) -> str:
    if not arguments:
        return ""
    return " ".join(f"{k}={_clip(str(v), 40)}" for k, v in arguments.items())


def _clip(text: str, width: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
