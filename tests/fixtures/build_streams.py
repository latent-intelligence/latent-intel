"""Regenerate the recorded event streams in `streams/`.

    uv run python tests/fixtures/build_streams.py

These are the reason the frontends can be tested at all. A renderer consuming a recorded
stream needs no store, no network and no model, so the claim that frontends are
interchangeable is checkable now rather than when the web client is written. They are
also how that web client gets built later against a backend that is not running.

Everything is deterministic — UUIDs derive from a fixed namespace, timestamps step by a
constant — so regenerating produces a byte-identical file and a real change shows up
as a readable diff instead of a wall of new identifiers.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from latent_intel import events as ev  # noqa: E402
from latent_intel.models import (  # noqa: E402
    Approval,
    Artifact,
    AttachedSource,
    Capability,
    Descriptor,
    Doc,
    Effect,
    Hit,
    Provenance,
    Skill,
    WebScope,
)
from latent_intel.session import _fingerprint  # noqa: E402
from latent_intel.settings import Settings  # noqa: E402

NS = UUID("6f1d5b2a-0000-4000-8000-000000000000")
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
OUT = Path(__file__).parent / "streams"
#: Published samples of the recording format, for consumers to copy into their own tests
#: (house-rules/integration.md, "fixtures, not imports"). Each opens with its header.
FORMATS = Path(__file__).parent / "formats" / "recording"


class Recorder:
    """An Emitter with the clock and the identifiers pinned."""

    def __init__(self, name: str, directory: Path = OUT) -> None:
        self.name = name
        self.directory = directory
        self.session_id = uuid5(NS, f"{name}/session")
        self.operation_id = uuid5(NS, f"{name}/operation")
        self.events: list[ev.BaseEvent] = []

    def add(self, cls: type[Any], parent: Any = None, **fields: Any) -> Any:
        n = len(self.events) + 1
        event = cls(
            event_id=uuid5(NS, f"{self.name}/{n}"),
            session_id=self.session_id,
            operation_id=self.operation_id,
            parent_id=parent.event_id if parent is not None else None,
            sequence=n,
            ts=T0 + timedelta(milliseconds=120 * n),
            **fields,
        )
        self.events.append(event)
        return event

    def write(self) -> Path:
        path = self.directory / f"{self.name}.jsonl"
        path.write_text(
            "".join(ev.dump_event(e) + "\n" for e in self.events), encoding="utf-8"
        )
        return path


def prov(source: str, ref: str, method: str, **kw: Any) -> Provenance:
    return Provenance(source_id=source, ref=ref, method=method, **kw)


def search_stream() -> Recorder:
    """Two sources answering one query — what a merged ranking would misrepresent."""
    r = Recorder("search")
    r.add(ev.UserMessage, text="search context collapse")
    r.add(
        ev.RetrievalStarted,
        query="context collapse",
        sources=["design", "papers"],
        filters={"limit": 5},
    )
    r.add(
        ev.RetrievalResult,
        source_id="design",
        query="context collapse",
        hits=[
            Hit(
                ref="design:context-collapse",
                source_id="design",
                title="Context collapse",
                kind="concept",
                lead=(
                    "Asking a model to rewrite an accumulated context end to end "
                    "progressively destroys it: one measured run fell from 18,282 "
                    "tokens to 122, below the no-context baseline."
                ),
                score=12.0,
                provenance=prov(
                    "design",
                    "design:context-collapse",
                    "search",
                    origin="corpus:raw/ace-2025.md",
                    as_of="2026-07-14",
                ),
            ),
            Hit(
                ref="design:zhang2025-agentic-context-engineering",
                source_id="design",
                title="Agentic context engineering",
                kind="source",
                lead=(
                    "Argues that adapting a model through its context rather than its "
                    "weights is both cheaper and more inspectable."
                ),
                score=6.0,
                provenance=prov(
                    "design",
                    "design:zhang2025-agentic-context-engineering",
                    "search",
                    as_of="2026-07-14",
                ),
            ),
        ],
    )
    r.add(
        ev.RetrievalResult,
        source_id="papers",
        query="context collapse",
        hits=[
            Hit(
                ref="papers:chunk-8812",
                source_id="papers",
                title="smith2019-methods.pdf, p. 14",
                kind="chunk",
                lead="…rewriting accumulated state at each step loses detail that…",
                # A cosine, not a count. Deliberately close to the lexical score above:
                # a merged list would sort these together and imply they are comparable.
                score=0.81,
                provenance=prov("papers", "papers:chunk-8812", "search", origin="p.14"),
            )
        ],
        truncated=True,
    )
    return r


def retrieval_stream() -> Recorder:
    """A single fetch — the detail rung."""
    r = Recorder("retrieval")
    r.add(ev.UserMessage, text="open design:context-collapse")
    r.add(
        ev.DocumentFetched,
        doc=Doc(
            ref="design:context-collapse",
            source_id="design",
            title="Context collapse",
            kind="concept",
            body=(
                "Asking a model to rewrite an accumulated context end to end "
                "progressively destroys it.\n\n"
                "## What was measured\n\n"
                "An unbounded rewrite collapsed a context from 18,282 tokens to 122 — "
                "below the no-context baseline, meaning the compression was worse than "
                "having no context at all.\n\n"
                "## Why a ratio target does not fix it\n\n"
                "A compiler given a 75% ratio target delivered ~35% and tripled "
                "catastrophic failures. The same compiler with an external probe "
                "measuring what was dropped recovered ~80% of the loss.\n"
            ),
            metadata={"status": "active", "captured": "2026-07-14"},
            provenance=prov(
                "design",
                "design:context-collapse",
                "fetch",
                origin="corpus:raw/ace-2025.md",
                as_of="2026-07-14",
            ),
        ),
    )
    return r


def tool_call_stream() -> Recorder:
    """A read-only tool, nested under the turn that caused it."""
    r = Recorder("tool_call")
    r.add(ev.UserMessage, text="what does the wiki say about compaction?")
    started = r.add(
        ev.ToolStarted,
        tool="design.wiki_search",
        source_id="design",
        arguments={"query": "compaction", "limit": 3},
        effect=Effect.EXTERNAL_READ.value,
    )
    r.add(
        ev.ToolResult,
        parent=started,
        tool="design.wiki_search",
        source_id="design",
        ok=True,
        output="[concept] context-collapse — Asking a model to rewrite…",
        duration_ms=48,
    )
    r.add(
        ev.AgentCompleted,
        text="Compaction is safe only when a non-model mechanism bounds it.",
        citations=["design:context-collapse"],
        usage={"input_tokens": 2140, "output_tokens": 96},
    )
    return r


def approval_stream() -> Recorder:
    """A write, gated on a declared effect rather than a guess about a name."""
    r = Recorder("approval")
    r.add(ev.UserMessage, text="file that finding as a page")
    requested = r.add(
        ev.ApprovalRequested,
        approval=Approval(
            id="apr-1",
            tool="design.wiki_write",
            effect=Effect.LOCAL_WRITE,
            summary="Create pages/compaction-bounds.md in the design wiki",
            arguments={"key": "compaction-bounds", "type": "concept"},
        ),
    )
    r.add(ev.ApprovalResolved, parent=requested, approval_id="apr-1", granted=True)
    started = r.add(
        ev.ToolStarted,
        tool="design.wiki_write",
        source_id="design",
        arguments={"key": "compaction-bounds"},
        effect=Effect.LOCAL_WRITE.value,
    )
    r.add(
        ev.ToolResult,
        parent=started,
        tool="design.wiki_write",
        source_id="design",
        ok=True,
        output="wrote pages/compaction-bounds.md",
        duration_ms=12,
    )
    r.add(
        ev.ArtifactCreated,
        artifact=Artifact(
            id="art-1",
            kind="page",
            title="Compaction bounds",
            path="pages/compaction-bounds.md",
            preview="Compaction is safe exactly when a non-model mechanism bounds it.",
        ),
    )
    return r


def failure_stream() -> Recorder:
    """Failures travel as events.

    A frontend iterating a stream over a socket cannot catch an exception raised inside
    the generator. Anything a person should see has to be in the stream.
    """
    r = Recorder("failure")
    r.add(ev.UserMessage, text="ask why retrieval is slow")
    r.add(
        ev.AgentFailed,
        message="no agent runtime configured",
        kind="runtime_unavailable",
        remedy=(
            "choose one with `/runtime claude-cli` in the shell, or set `runtime:` in "
            "~/.config/latent-intel/config.yaml. "
            "Run `intel doctor` to see which are available."
        ),
    )
    return r


def agent_turn_stream() -> Recorder:
    """The full vocabulary in one file — what a renderer has to survive."""
    r = Recorder("agent_turn")
    r.add(
        ev.SourceConnected,
        descriptor=Descriptor(
            id="design",
            kind="wiki",
            title="DESIGN",
            capabilities=[Capability.SEARCH, Capability.FETCH, Capability.TOOLS],
            count=294,
            unit="pages",
            freshness="2026-08-21",
        ),
    )
    r.add(ev.UserMessage, text="which sources disagree about context compaction?")
    r.add(
        ev.RetrievalStarted, query="context compaction disagreement", sources=["design"]
    )
    r.add(
        ev.RetrievalResult,
        source_id="design",
        query="context compaction disagreement",
        hits=[
            Hit(
                ref="design:ace-contradiction",
                source_id="design",
                title="ACE contradiction",
                kind="contradiction",
                lead="Two sources disagree on whether compaction is recoverable.",
                score=9.0,
                provenance=prov("design", "design:ace-contradiction", "search"),
            )
        ],
    )
    r.add(
        ev.ContextAdded,
        ref="design:ace-contradiction",
        source_id="design",
        title="ACE contradiction",
        tokens=812,
    )
    for chunk in (
        "Two sources disagree. ",
        "The ACE paper argues compaction is recoverable ",
        "with an external probe; the reference-corpus work finds ",
        "the loss is permanent past a threshold.",
    ):
        r.add(ev.AssistantToken, text=chunk)
    r.add(
        ev.AgentCompleted,
        text=(
            "Two sources disagree. The ACE paper argues compaction is recoverable with "
            "an external probe; the reference-corpus work finds the loss is permanent "
            "past a threshold."
        ),
        citations=["design:ace-contradiction", "design:context-collapse"],
        # This turn emitted AssistantTokens above, so a terminal renderer has already
        # shown the answer; `streamed` is what stops it printing a second time.
        streamed=True,
        usage={"input_tokens": 4820, "output_tokens": 142},
    )
    return r


def recorded_ask() -> Recorder:
    """A recording of one eval turn: header, question, a search tool, a cited answer."""
    r = Recorder("ask", FORMATS)
    r.add(
        ev.RunContext,
        purpose="eval",
        title="compaction, haiku",
        latent_intel_version="0.1.0",
        runtime="claude-agent-sdk",
        host="anthropic",
        model="claude-haiku-4-5",
        approval="auto",
        # Internal sources only — the scope an eval of retrieval runs under.
        web=WebScope(),
        project="research",
        # Through the writer's own code, so the digests are ones it can produce.
        context=_fingerprint(
            Settings(
                persona="You are an archivist.",
                skills=[Skill(name="citation-style", body="Cite inline.")],
            )
        ),
        sources=[AttachedSource(id="design", kind="wiki")],
        case_id="q17",
        variant_id="haiku",
        epoch=1,
    )
    r.add(ev.UserMessage, text="is context compaction recoverable?")
    started = r.add(
        ev.ToolStarted,
        tool="design.wiki_search",
        source_id="design",
        arguments={"query": "compaction recoverable", "limit": 3},
        effect=Effect.EXTERNAL_READ.value,
    )
    r.add(
        ev.ToolResult,
        parent=started,
        tool="design.wiki_search",
        source_id="design",
        output=(
            "[contradiction] design:ace-contradiction — ACE contradiction\n"
            "  Two sources disagree on whether compaction is recoverable.\n\n"
            "[concept] design:context-collapse — Context collapse\n"
            "  Rewriting an accumulated context end to end destroys it."
        ),
        refs=["design:ace-contradiction", "design:context-collapse"],
        duration_ms=140,
    )
    r.add(
        ev.AgentCompleted,
        text=(
            "Only with an external probe: unbounded rewriting destroys context "
            "(design:context-collapse), and the sources disagree on recovery "
            "(design:ace-contradiction)."
        ),
        citations=["design:context-collapse", "design:ace-contradiction"],
        usage={"input_tokens": 9100, "output_tokens": 640},
        cost_usd=0.012,
    )
    return r


def recorded_search() -> Recorder:
    """A recording of a search, shared: the header, then the search stream."""
    r = Recorder("search", FORMATS)
    r.add(
        ev.RunContext,
        title="two sources, one query",
        latent_intel_version="0.1.0",
        web=WebScope(),
        context=_fingerprint(Settings()),
        sources=[
            AttachedSource(id="design", kind="wiki"),
            AttachedSource(id="papers", kind="vector"),
        ],
    )
    for event in search_stream().events:
        envelope = {"event_id", "session_id", "operation_id", "parent_id", "sequence"}
        fields = event.model_dump(exclude=envelope | {"ts"})
        r.add(type(event), **fields)
    return r


BUILDERS = (
    search_stream,
    retrieval_stream,
    tool_call_stream,
    approval_stream,
    failure_stream,
    agent_turn_stream,
    recorded_ask,
    recorded_search,
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    FORMATS.mkdir(parents=True, exist_ok=True)
    for build in BUILDERS:
        recorder = build()
        path = recorder.write()
        print(f"{path.name:<22} {len(recorder.events):>2} events")


if __name__ == "__main__":
    main()
