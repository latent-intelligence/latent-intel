"""The published sample of `latent-intel.eval-run`, written by `batch` itself.

    uv run python tests/fixtures/build_streams.py

Three cases through two variants: `haiku` on `anthropic-sdk`, which reports no cost,
and `sonnet` on `claude-agent-sdk`, which does. A fake stands in for both runtimes.
Both variants answer q1 with a search (a `tool_result` with refs and a
`retrieval_result`) and two citations, one retrieved and one not. On q2, `haiku`
fails and `sonnet` stops at the round ceiling with the answer so far, which is `ok`.
The batch is interrupted before q3, so the manifest shows every status (`ok`,
`failed`, `pending`), and only the four runs that happened have a recording. Run id,
identifiers, clocks and the version are pinned afterwards, so regenerating gives
byte-identical files and a test can hold the committed sample to what the code writes.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import anyio

from latent_intel import batch
from latent_intel import config as config_module
from latent_intel import events as ev
from latent_intel import registry as registry_module
from latent_intel import settings as settings_module
from latent_intel.agent import base as agent
from latent_intel.models import Hit, Provenance

SAMPLE = Path(__file__).parent / "formats" / "eval-run"
RUN_ID = "20260821T120000Z-5a3c1e"
VERSION = "0.1.0"
NS = UUID("6f1d5b2a-0000-4000-8000-0000000e7a1e")
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)

CASES = """\
format: latent-eval.dataset
format_version: 1
name: tides
cases:
  - id: q1
    input: {question: "What causes the tides?"}
    reference: The Moon's gravity, with the Sun's.
    metadata: {stratum: single-source}
  - id: q2
    input: {question: "Why are there two high tides a day?"}
    metadata: {stratum: single-source}
  - id: q3
    input: {question: "When are spring tides?"}
    metadata: {stratum: single-source}
"""

VARIANTS = """\
variants:
  - {id: haiku, runtime: anthropic-sdk, options: {model: claude-haiku-4-5}}
  - {id: sonnet, runtime: claude-agent-sdk, options: {model: claude-sonnet-4-5}}
"""


class _Interrupted(Exception):
    """Stops the batch after four of its six runs, the way an interrupt would."""


class SampleRuntime:
    """Stands in for `anthropic-sdk`, which reports no cost."""

    id = "anthropic-sdk"
    host = "anthropic"
    #: What one answered turn costs, where the runtime reports it.
    cost: float | None = None

    def __init__(self, **options: Any) -> None:
        self.model = options.get("model")

    def available(self) -> bool:
        return True

    async def stream(self, messages, tools, *, emitter, **options):  # type: ignore[no-untyped-def]
        question = messages[-1].text
        if "two high tides" in question and self.cost is None:
            yield emitter.emit(
                ev.AgentFailed,
                message="the endpoint returned 529: overloaded",
                kind="api_error",
                remedy="ask again",
            )
            return
        started = emitter.emit(
            ev.ToolStarted,
            tool="notes.search",
            source_id="notes",
            arguments={"query": "tides"},
            effect="external_read",
        )
        yield started
        yield emitter.nested(started).emit(
            ev.ToolResult,
            tool="notes.search",
            source_id="notes",
            output="notes:tides — Tides\n  The Moon's gravity raises the tides.",
            refs=["notes:tides"],
            duration_ms=140,
        )
        yield emitter.emit(
            ev.RetrievalResult,
            source_id="notes",
            query="tides",
            hits=[
                Hit(
                    ref="notes:tides",
                    source_id="notes",
                    title="Tides",
                    lead="The Moon's gravity raises the tides.",
                    score=3.0,
                    provenance=Provenance(
                        source_id="notes", method="search", ref="notes:tides"
                    ),
                )
            ],
        )
        if "two high tides" in question:
            # The round ceiling, reached with part of an answer: `ok`, not an error.
            text = "One bulge faces the Moon (notes:tides); the second I did not reach."
            yield emitter.emit(ev.AssistantToken, text=text)
            yield emitter.emit(
                ev.AgentCompleted,
                text=text,
                streamed=True,
                citations=["notes:tides"],
                usage={"input_tokens": 6400, "output_tokens": 310},
                cost_usd=self.cost * 2,
                stopped="tool_rounds",
            )
            return
        # `notes:neap` is cited and never retrieved: what a cited-refs check catches.
        text = (
            "The Moon's gravity, with the Sun's (notes:tides); neap tides (notes:neap)."
        )
        yield emitter.emit(ev.AssistantToken, text=text)
        yield emitter.emit(
            ev.AgentCompleted,
            text=text,
            streamed=True,
            citations=["notes:tides", "notes:neap"],
            usage={"input_tokens": 2100, "output_tokens": 180},
            cost_usd=self.cost,
        )


class CostedRuntime(SampleRuntime):
    """Stands in for `claude-agent-sdk`, which reports what a turn cost."""

    id = "claude-agent-sdk"
    cost = 0.0042


@contextmanager
def _isolated(work: Path) -> Iterator[None]:
    """An install of its own: config, deployments, registry and the fake runtime."""
    names = {
        config_module.ENV_HOME: str(work / "home"),
        "LATENT_INTEL_PROJECTS": str(work / "projects"),
        "LATENT_INTEL_PROJECT": "research",
        registry_module.ENV_VAR: str(work / "registry.yaml"),
    }
    saved = {name: os.environ.get(name) for name in names}
    kinds = agent.available_kinds
    os.environ.update(names)
    agent.available_kinds = lambda: {  # type: ignore[assignment]
        "anthropic-sdk": SampleRuntime,
        "claude-agent-sdk": CostedRuntime,
    }
    settings_module.invalidate()
    try:
        yield
    finally:
        agent.available_kinds = kinds  # type: ignore[assignment]
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        settings_module.invalidate()


def run(work: Path) -> Path:
    """Run the batch beneath `work` and return the run's directory, unpinned."""
    (work / "projects").mkdir(parents=True)
    corpus = work / "notes"
    corpus.mkdir()
    (corpus / "tides.md").write_text("# Tides\n\nThe Moon's gravity.\n")
    (work / "projects" / "research.yaml").write_text(
        f"name: research\nroot: {work / 'out'}\n"
        f"sources:\n  - {{id: notes, kind: files, target: {corpus}}}\n"
    )
    (work / "cases.yaml").write_text(CASES)
    (work / "variants.yaml").write_text(VARIANTS)

    with _isolated(work):
        planned = batch.plan(work / "cases.yaml", work / "variants.yaml")
        planned.run_id = RUN_ID
        planned.directory = work / "out" / "runs" / RUN_ID

        def stop(recording: batch.Recording) -> None:
            if (recording.case_id, recording.variant_id) == ("q2", "sonnet"):
                raise _Interrupted

        with contextlib.suppress(_Interrupted):
            anyio.run(lambda: batch.execute(planned, progress=stop))
    return planned.directory


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|\+00:00)")
_VERSION = re.compile(r'("latent_intel_version": ?")[^"]*(")')


def pinned(directory: Path) -> dict[str, str]:
    """Every file of a run, by path relative to it, with what varies pinned:
    identifiers and clocks in order of appearance, and the package version."""
    paths = ["manifest.json"] + sorted(
        str(p.relative_to(directory)) for p in directory.rglob("*.jsonl")
    )
    uuids: dict[str, str] = {}
    clocks: list[str] = []

    def uuid(match: re.Match[str]) -> str:
        return uuids.setdefault(
            match.group(0), str(uuid5(NS, f"eval-run/{len(uuids) + 1}"))
        )

    def clock(match: re.Match[str]) -> str:
        clocks.append(match.group(0))
        at = T0 + timedelta(milliseconds=120 * len(clocks))
        if match.group(1) == "Z":
            return at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return at.isoformat()

    files: dict[str, str] = {}
    for path in paths:
        text = (directory / path).read_text(encoding="utf-8")
        text = _VERSION.sub(rf"\g<1>{VERSION}\g<2>", text)
        files[path] = _TS.sub(clock, _UUID.sub(uuid, text))
    return files


def write(target: Path = SAMPLE) -> list[Path]:
    """Regenerate the published sample in `target`."""
    with tempfile.TemporaryDirectory() as work:
        files = pinned(run(Path(work)))
    shutil.rmtree(target, ignore_errors=True)
    written = []
    for path, text in files.items():
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        written.append(out)
    return written
