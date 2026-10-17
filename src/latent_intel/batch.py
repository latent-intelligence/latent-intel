"""Batch eval runs — cases × variants × repeats, each one recorded for `latent-eval`.

Run once, score many: this writes recordings and a manifest that names them, and
`latent-eval` reads them from disk. Neither imports the other.

**The case file is read as little as possible.** `id`, `input.question` and
`metadata`; never `reference`, which is the scorer's business. The less this reads,
the less a change to that format can break, and the answer never reaches a model by
accident.

**A variant names a deployment.** Context engineering — persona, skills, rules,
agents — lives there, so a variant is a deployment, a runtime and that runtime's
options. Each run is a fresh `Session` on that deployment, loaded by path without
becoming the active one.

**Runs go beneath the starting deployment's `root:`**, at `<root>/runs/<run-id>/`.
Refused before anything runs when there is no root to honour, or when the root lies in
the installed package: the built-in deployment's directory is the source tree.

**Planning spends nothing.** `plan` reads and checks every file, and builds each
variant's runtime to read its host, without attaching a source or asking a question.
`execute` is the only part that spends.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from . import __version__
from . import config as config_module
from . import events as ev
from . import project as project_module
from . import settings as settings_module
from .agent import base as agent
from .commands import Ask
from .models import SourceRequest
from .session import Session

log = logging.getLogger(__name__)

MANIFEST_FORMAT = "latent-intel.eval-run"
MANIFEST_VERSION = 1

MANIFEST_NAME = "manifest.json"

#: The case files this reads — `latent-eval`'s, published as its `dataset.md`.
DATASET_FORMAT = "latent-eval.dataset"
DATASET_VERSION = 1
STAMP = ("format", "format_version")
CASE_FIELDS = frozenset({"id", "input", "reference", "metadata"})

#: Said when two ids differ only in case: on a case-insensitive filesystem, such as
#: macOS's default, they name one file, and the second run would overwrite the first.
CASE_NOTE = " (ids differing only in case name the same file)"

#: A case, variant or run id becomes part of a file name.
SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

VARIANT_KEYS = frozenset({"id", "deployment", "runtime", "options"})

#: The one host that runs on this machine and bills nobody.
FREE_HOSTS = frozenset({"local"})

#: Where the package is installed; a root beneath it would write into the source tree.
PACKAGE = Path(__file__).resolve().parent


class BatchError(Exception):
    """A batch that cannot run as asked — refused before anything runs."""


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Variant:
    id: str
    #: The deployment file, resolved; never written to the manifest.
    deployment: Path
    deployment_name: str
    runtime: str
    options: dict[str, Any]
    #: As the built runtime holds them, for the dry run to show.
    host: str | None
    model: str | None
    paid: bool
    #: Why its runtime cannot run on this machine, or None. A dry run shows it;
    #: `execute` refuses it.
    reason: str | None = None


@dataclass
class Recording:
    case_id: str
    variant_id: str
    epoch: int
    status: str = "pending"

    @property
    def path(self) -> str:
        """Relative to the run's directory."""
        return f"{self.variant_id}/{self.case_id}.{self.epoch}.jsonl"


@dataclass
class Plan:
    run_id: str
    #: `<root>/runs/<run-id>` — a `Path` or a `UPath`.
    directory: Path
    deployment: str
    cases: list[Case]
    cases_name: str
    cases_sha256: str
    #: False for a file written before `latent-eval.dataset` had a stamp.
    cases_stamped: bool
    variants: list[Variant]
    repeat: int
    #: Epoch-major, then case, then variant: an interrupted batch has every variant's
    #: answer to the same questions, which is what a comparison needs.
    recordings: list[Recording]

    @property
    def count(self) -> int:
        return len(self.recordings)

    def manifest(self, started: str, finished: str | None) -> dict[str, Any]:
        """Names only, never a path: variant options are left out, because a `cwd` or
        a binary path is a location, and each recording's header already names the
        model and host that answered."""
        return {
            "format": MANIFEST_FORMAT,
            "format_version": MANIFEST_VERSION,
            "run_id": self.run_id,
            "latent_intel_version": __version__,
            "deployment": self.deployment,
            "started": started,
            "finished": finished,
            "cases": {
                "name": self.cases_name,
                "sha256": self.cases_sha256,
                "count": len(self.cases),
            },
            "repeat": self.repeat,
            "variants": [
                {"id": v.id, "deployment": v.deployment_name, "runtime": v.runtime}
                for v in self.variants
            ],
            "recordings": [
                {
                    "path": r.path,
                    "case_id": r.case_id,
                    "variant_id": r.variant_id,
                    "epoch": r.epoch,
                    "status": r.status,
                }
                for r in self.recordings
            ],
        }


def new_run_id() -> str:
    """UTC to the second, then six hex digits: sorts by time, never collides."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def plan(cases: Path, variants: Path, *, repeat: int = 1) -> Plan:
    """Read and check everything a batch needs. Spends nothing and writes nothing."""
    if repeat < 1:
        raise BatchError(f"--repeat must be at least 1, not {repeat}")
    resolved = settings_module.load()
    project = resolved.project
    remedy = "`intel deployment use <name>`, or a `root:` in the deployment"
    if project is None:
        why = f" ({'; '.join(resolved.problems)})" if resolved.problems else ""
        raise BatchError(f"no active deployment to write runs beneath{why} — {remedy}")
    root = project.root
    if root is None:
        why = "; ".join(p for p in project.problems if p.startswith("root:"))
        raise BatchError(
            f"deployment '{project.name}' has no usable root ({why}) — {remedy}"
        )
    if not hasattr(root, "protocol") and root.resolve().is_relative_to(PACKAGE):
        raise BatchError(
            f"deployment '{project.name}' is the installed package's own; its runs "
            f"would land in the source tree — {remedy}"
        )

    loaded, stamped = load_cases(cases)
    chosen = load_variants(variants, default=project.path)
    run_id = new_run_id()
    return Plan(
        run_id=run_id,
        directory=root / "runs" / run_id,
        deployment=project.name,
        cases=loaded,
        cases_name=cases.name,
        cases_sha256=hashlib.sha256(cases.read_bytes()).hexdigest(),
        cases_stamped=stamped,
        variants=chosen,
        repeat=repeat,
        recordings=[
            Recording(case.id, variant.id, epoch)
            for epoch in range(1, repeat + 1)
            for case in loaded
            for variant in chosen
        ],
    )


def load_cases(path: Path) -> tuple[list[Case], bool]:
    """`latent-eval.dataset` v1, YAML or JSON Lines, and whether the file is stamped.

    A stamped file is held to the stamp: another format or version is refused. An
    unstamped one is read as `latent-eval` reads it — `input` defaults to `{}`, and
    YAML's `context:` stands in for `input:` — because the spec guarantees only its
    `id`. Either way a case with no question is refused before anything runs, every
    such case named at once: skipping one would score a smaller set than was asked.
    """
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            rows: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
            stamps = [isinstance(r, dict) and _is_stamped(r) for r in rows]
            if any(stamps) and not all(stamps):
                raise BatchError(f"{path.name}: mixes stamped and unstamped lines")
            stamped = bool(stamps) and all(stamps)
            for number, row in enumerate(rows if stamped else [], start=1):
                _stamp(row, f"{path.name}:{number}")
                _stamped_case(
                    {k: v for k, v in row.items() if k not in STAMP},
                    f"{path.name}:{number}",
                )
        elif path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text) or {}
            if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
                raise BatchError(
                    f"{path.name}: a dataset is a mapping with a `cases:` list"
                )
            stamped = _is_stamped(data)
            rows = data["cases"]
            if stamped:
                _stamp(data, path.name)
                if unknown := sorted(set(data) - {*STAMP, "name", "cases"}):
                    raise BatchError(
                        f"{path.name}: unknown key(s) {', '.join(unknown)}"
                    )
                for number, row in enumerate(rows, start=1):
                    _stamped_case(row, f"{path.name}: case {number}")
        else:
            raise BatchError(f"{path.name}: cases are .yaml, .yml or .jsonl")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise BatchError(f"{path.name}: {exc}") from exc

    cases: list[Case] = []
    unasked: list[str] = []
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or "id" not in row:
            raise BatchError(f"{path.name}: case {number} is not a mapping with an id")
        case_id = _safe(str(row["id"]), f"{path.name}: case id")
        if any(c.id.casefold() == case_id.casefold() for c in cases):
            raise BatchError(f"{path.name}: case '{case_id}' appears twice{CASE_NOTE}")
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise BatchError(
                f"{path.name}: case '{case_id}': metadata is not a mapping"
            )
        legacy = not stamped and path.suffix != ".jsonl" and "input" not in row
        value = row.get("context" if legacy else "input")
        question = value.get("question") if isinstance(value, dict) else None
        if not isinstance(question, str) or not question.strip():
            unasked.append(case_id)
            continue
        cases.append(Case(case_id, question, metadata))
    if unasked:
        raise BatchError(
            f"{path.name}: no `input.question` to ask in case(s) {', '.join(unasked)}"
        )
    if not cases:
        raise BatchError(f"{path.name}: no cases")
    return cases, stamped


def _is_stamped(data: dict[str, Any]) -> bool:
    """As `latent-eval` decides it: a bare `format` key may be an old file's own."""
    return "format_version" in data or data.get("format") == DATASET_FORMAT


def _stamped_case(row: Any, where: str) -> None:
    """What `latent-eval` holds a stamped case to, so a batch never spends on a file
    its scorer will then refuse."""
    if not isinstance(row, dict):
        raise BatchError(f"{where}: a case is a mapping")
    problems = []
    if unknown := sorted(set(row) - CASE_FIELDS):
        problems.append(
            f"unknown field(s) {', '.join(unknown)} (extras go in metadata)"
        )
    if not (isinstance(row.get("id"), str) and row["id"]):
        problems.append("id must be a non-empty string")
    if not isinstance(row.get("input"), dict):
        problems.append("input must be an object")
    if "metadata" in row and not isinstance(row["metadata"], dict):
        problems.append("metadata must be an object")
    if problems:
        raise BatchError(f"{where}: {'; '.join(problems)}")


def _stamp(row: Any, name: str) -> None:
    """Refuse a stamp naming another format, or a version this build does not know."""
    version = row.get("format_version") if isinstance(row, dict) else None
    if (
        not isinstance(row, dict)
        or row.get("format") != DATASET_FORMAT
        or isinstance(version, bool)
        or version != DATASET_VERSION
    ):
        found = (row.get("format"), version) if isinstance(row, dict) else row
        raise BatchError(
            f"{name}: stamped {found!r}, not {DATASET_FORMAT} v{DATASET_VERSION}"
        )


def _safe(value: str, what: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise BatchError(
            f"{what} '{value}' must be letters, digits, `.`, `_` or `-`, and not start "
            f"with `.` — it names a file"
        )
    return value


def load_variants(path: Path, *, default: Path) -> list[Variant]:
    """Each variant: an id, a deployment (a name, or a path relative to `path`;
    `default` when absent), a runtime, and options laid over the deployment's own."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BatchError(f"{path.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
        raise BatchError(f"{path.name}: expected a mapping with a `variants:` list")
    if unknown := sorted(set(data) - {"variants"}):
        raise BatchError(f"{path.name}: unknown key(s) {', '.join(unknown)}")

    variants: list[Variant] = []
    for number, row in enumerate(data["variants"], start=1):
        if not isinstance(row, dict) or "id" not in row:
            raise BatchError(
                f"{path.name}: variant {number} is not a mapping with an id"
            )
        variant_id = _safe(str(row["id"]), f"{path.name}: variant id")
        label = f"{path.name}: variant '{variant_id}'"
        if unknown := sorted(set(row) - VARIANT_KEYS):
            raise BatchError(
                f"{label}: unknown key(s) {', '.join(unknown)} — expected "
                f"{', '.join(sorted(VARIANT_KEYS))}"
            )
        if any(v.id.casefold() == variant_id.casefold() for v in variants):
            raise BatchError(f"{label} appears twice{CASE_NOTE}")
        if variant_id.casefold() == MANIFEST_NAME:
            raise BatchError(f"{label}: '{MANIFEST_NAME}' is the manifest's own name")
        if not row.get("runtime"):
            raise BatchError(f"{label} names no runtime")
        options = row.get("options") or {}
        if not isinstance(options, dict):
            raise BatchError(f"{label}: options is not a mapping")
        if "approval" in options:
            raise BatchError(
                f"{label}: approval is not a runtime option — set it in the deployment"
            )
        deployment = (
            _deployment(str(row["deployment"]), path.parent, label)
            if row.get("deployment")
            else default
        )
        variants.append(
            _variant(variant_id, deployment, str(row["runtime"]), options, label)
        )
    if not variants:
        raise BatchError(f"{path.name}: no variants")
    return variants


def _deployment(value: str, base: Path, label: str) -> Path:
    """A path when it looks like one, relative to the variants file; else a name."""
    if value.endswith((".yaml", ".yml")) or "/" in value or "\\" in value:
        return (base / Path(value).expanduser()).resolve()
    config = config_module.load()
    found, _ = project_module.discover(config.deployment_paths, config_module.home())
    if value not in found:
        known = ", ".join(sorted(found)) or "none found"
        raise BatchError(f"{label}: no deployment '{value}' (found: {known})")
    return found[value]


def _variant(
    variant_id: str, deployment: Path, kind: str, options: dict[str, Any], label: str
) -> Variant:
    """Load the deployment and build the runtime once, to refuse what cannot run and
    to read the host it would dial. Building reads the environment, not the network."""
    try:
        project = project_module.load(deployment)
    except project_module.ProjectError as exc:
        raise BatchError(f"{label}: {exc}") from exc
    # A source skipped for a missing `target:` is only a problem, and the variant
    # would run without it: a smaller variant, compared as if it were the declared one.
    if problems := settings_module.load(path=deployment).problems:
        raise BatchError(f"{label}: deployment '{project.name}': {'; '.join(problems)}")
    try:
        runtime = _build(deployment, kind, options)
    except Exception as exc:  # noqa: BLE001 — any build failure refuses the batch
        raise BatchError(f"{label}: {exc}") from exc
    reason = None
    if not runtime.available():
        why = (
            runtime.unavailable_reason()
            if isinstance(runtime, agent.Diagnosable)
            else None
        )
        reason = f"'{kind}' cannot run here" + (f" — {why}" if why else "")
    host = _attribute(runtime, "host")
    return Variant(
        id=variant_id,
        deployment=deployment,
        deployment_name=project.name,
        runtime=kind,
        options=dict(options),
        host=host,
        model=_attribute(runtime, "model"),
        paid=host not in FREE_HOSTS,
        reason=reason,
    )


def _build(deployment: Path, kind: str, options: dict[str, Any]) -> agent.Runtime:
    """The deployment's options for this runtime, the variant's over them, and the
    approval resolved across the layers last, as `Session` builds one."""
    resolved = settings_module.load(path=deployment)
    return agent.build(
        kind,
        **{**resolved.runtime_options(kind), **options, "approval": resolved.approval},
    )


def _attribute(runtime: agent.Runtime, key: str) -> str | None:
    value = getattr(runtime, key, None)
    return str(value) if value else None


async def execute(
    planned: Plan, *, progress: Callable[[Recording], None] | None = None
) -> dict[str, Any]:
    """Run every recording in order, one fresh session each. Returns the manifest.

    The manifest is written before the first run and after each one, so a batch cut
    short still names what exists. A run that fails is marked and the next one starts:
    one host error must not cost the rest of a paid batch. Storage that cannot be
    written is the exception, and stops the batch: there is nowhere to record it.
    """
    if blocked := [f"{v.id}: {v.reason}" for v in planned.variants if v.reason]:
        raise BatchError("; ".join(blocked))
    directory = planned.directory
    directory.mkdir(parents=True, exist_ok=True)
    started = _now()
    manifest_path = directory / MANIFEST_NAME

    def write(finished: str | None) -> dict[str, Any]:
        manifest = planned.manifest(started, finished)
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return manifest

    write(None)
    variants = {v.id: v for v in planned.variants}
    cases = {c.id: c for c in planned.cases}
    for recording in planned.recordings:
        ok = await _one(
            directory,
            recording,
            cases[recording.case_id],
            variants[recording.variant_id],
        )
        recording.status = "ok" if ok else "failed"
        write(None)
        if progress is not None:
            progress(recording)
    return write(_now())


async def _one(
    directory: Path, recording: Recording, case: Case, variant: Variant
) -> bool:
    """One case, one variant, one epoch, into its own file. True when it answered.

    Every recording opens with a header, a failed one included, and nothing a run
    raises — a session that cannot be built, a connector that raises anything at all,
    one that will not close — stops the next run.
    """
    target = directory / recording.path
    target.parent.mkdir(parents=True, exist_ok=True)
    update = {
        "case_id": case.id,
        "variant_id": variant.id,
        "epoch": recording.epoch,
        "metadata": case.metadata,
    }
    session: Session | None = None
    with target.open("w", encoding="utf-8") as handle:

        def tee(event: ev.BaseEvent) -> None:
            handle.write(ev.dump_event(event) + "\n")
            handle.flush()

        headed = False
        try:
            session = Session(
                runtime=_build(variant.deployment, variant.runtime, variant.options),
                deployment=variant.deployment,
            )
            resolved = settings_module.load(path=variant.deployment)
            failures = await session.connect_many(
                [
                    SourceRequest(
                        spec=spec.spec,
                        kind=None if spec.by_name else spec.kind,
                        source_id=spec.id,
                        remote=spec.remote,
                        options=spec.options,
                    )
                    for spec in resolved.sources
                ]
            )
            tee(session.run_context("eval").model_copy(update=update))
            headed = True
            if failures:
                tee(
                    ev.Emitter(session.session_id).emit(
                        ev.AgentFailed,
                        message="; ".join(failures),
                        kind="source_error",
                        remedy="a source the variant declares did not attach",
                    )
                )
                return False
            answered = failed = False
            async for event in session.run(Ask(prompt=case.prompt)):
                tee(event)
                answered = answered or isinstance(event, ev.AgentCompleted)
                failed = failed or isinstance(event, ev.AgentFailed)
            return answered and not failed
        except Exception as exc:  # noqa: BLE001 — recorded, and the batch goes on
            log.exception("run %s failed", recording.path)
            emitter = ev.Emitter(session.session_id if session else uuid4())
            if not headed:
                # What is known without a session: enough for a reader to place it.
                tee(
                    emitter.emit(
                        ev.RunContext,
                        purpose="eval",
                        latent_intel_version=__version__,
                        runtime=variant.runtime,
                        project=variant.deployment_name,
                        **update,
                    )
                )
            tee(emitter.emit(ev.AgentFailed, message=str(exc), kind="error"))
            return False
        finally:
            if session is not None:
                try:
                    await session.aclose()
                except Exception:  # noqa: BLE001 — the answer is already recorded
                    log.warning("run %s: a source failed to close", recording.path)


def _now() -> str:
    return datetime.now(UTC).isoformat()
