"""`batch` — cases × variants × repeats, one fresh session each, recorded beneath the
starting deployment's `root:`. A fake runtime throughout: no model, no network.

Each test gets its own `memory://` root, because the memory filesystem is one per
process and a path two tests share is a test that passes in one order only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema
import pytest
from upath import UPath

from latent_intel import batch
from latent_intel import events as ev
from latent_intel.agent import base as agent
from latent_intel.connectors import base as connectors_base
from latent_intel.connectors.files import FilesConnector

pytestmark = pytest.mark.anyio

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "schemas" / "recording.v1.json").read_text()
)

CASES = """\
format: latent-eval.dataset
format_version: 1
name: tides
cases:
  - id: q1
    input: {question: "What causes the tides?"}
    reference: The Moon's gravity — never read.
    metadata: {stratum: factual, labeller: ana}
  - id: q2
    input: {question: "When is high water?"}
    reference: SECRET-ANSWER
"""


class FakeRuntime:
    """Answers every question with its own variant's model name."""

    id = "fake"
    turns: list[list[str]] = []

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.model = options.get("model")
        self.host = options.get("host")

    def available(self) -> bool:
        return not self.options.get("unavailable")

    def unavailable_reason(self) -> str:
        return "set FAKE_KEY"

    async def stream(self, messages, tools, *, emitter, **options):  # type: ignore[no-untyped-def]
        type(self).turns.append([m.text for m in messages])
        if self.options.get("fail"):
            yield emitter.emit(ev.AgentFailed, message="the host fell over", kind="x")
            return
        yield emitter.emit(ev.AgentCompleted, text=f"answer from {self.model}")


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An active deployment with a `memory://` root and a files source, a second
    deployment beside the variants file, and two cases."""
    monkeypatch.setattr(agent, "available_kinds", lambda: {"fake": FakeRuntime})
    FakeRuntime.turns = []
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "tides.md").write_text("# Tides\n\nThe Moon.\n", encoding="utf-8")
    root = f"memory://eval-{uuid4().hex}"

    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "deploy.yaml").write_text(
        f"name: deploy\nroot: {root}\n"
        f"sources:\n  - {{id: notes, kind: files, target: {corpus}}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "deploy")

    work = tmp_path / "work"
    work.mkdir()
    (work / "plain.yaml").write_text("name: plain\n", encoding="utf-8")
    (work / "cases.yaml").write_text(CASES, encoding="utf-8")
    _variants(
        work,
        """\
variants:
  - {id: small, runtime: fake, options: {model: fake-small}}
  - {id: bare, deployment: ./plain.yaml, runtime: fake, options: {model: fake-bare}}
""",
    )
    return {"root": UPath(root), "work": work, "tmp": tmp_path}


def _variants(work: Path, body: str) -> Path:
    path = work / "variants.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _plan(setup: dict[str, Any], repeat: int = 1) -> batch.Plan:
    work = setup["work"]
    return batch.plan(work / "cases.yaml", work / "variants.yaml", repeat=repeat)


def _lines(path: UPath) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def test_two_cases_two_variants_two_repeats_write_eight_recordings(
    setup: dict[str, Any],
) -> None:
    planned = _plan(setup, repeat=2)
    manifest = await batch.execute(planned)

    directory = setup["root"] / "runs" / planned.run_id
    on_disk = json.loads((directory / "manifest.json").read_text())
    assert on_disk == manifest
    assert manifest["format"] == "latent-intel.eval-run"
    assert manifest["format_version"] == 1
    assert manifest["deployment"] == "deploy"
    assert manifest["repeat"] == 2
    assert manifest["finished"]
    assert manifest["cases"]["name"] == "cases.yaml"
    assert manifest["cases"]["count"] == 2
    assert [v["id"] for v in manifest["variants"]] == ["small", "bare"]
    assert [v["deployment"] for v in manifest["variants"]] == ["deploy", "plain"]

    entries = manifest["recordings"]
    assert len(entries) == 8
    assert {(e["case_id"], e["variant_id"], e["epoch"]) for e in entries} == {
        (c, v, n) for c in ("q1", "q2") for v in ("small", "bare") for n in (1, 2)
    }
    validator = jsonschema.Draft202012Validator(SCHEMA)
    for entry in entries:
        assert entry["status"] == "ok"
        lines = _lines(directory / entry["path"])
        for line in lines:
            validator.validate(line)
        header = lines[0]
        assert header["type"] == "run_context"
        assert header["purpose"] == "eval"
        assert (header["case_id"], header["variant_id"], header["epoch"]) == (
            entry["case_id"],
            entry["variant_id"],
            entry["epoch"],
        )
        assert header["project"] == (
            "deploy" if entry["variant_id"] == "small" else "plain"
        )
        expected = (
            {"stratum": "factual", "labeller": "ana"}
            if entry["case_id"] == "q1"
            else {}
        )
        assert header["metadata"] == expected
        assert [e["type"] for e in lines[1:]] == ["user_message", "agent_completed"]
        assert lines[-1]["text"] == f"answer from fake-{entry['variant_id']}"


async def test_a_run_reads_neither_the_reference_nor_writes_a_path(
    setup: dict[str, Any],
) -> None:
    planned = _plan(setup)
    manifest = await batch.execute(planned)
    directory = setup["root"] / "runs" / planned.run_id
    written = "".join(
        (directory / e["path"]).read_text() for e in manifest["recordings"]
    ) + json.dumps(manifest)
    assert "SECRET-ANSWER" not in written
    assert str(setup["tmp"]) not in written


async def test_each_run_is_a_fresh_session(setup: dict[str, Any]) -> None:
    """One turn's history must never reach the next case, or the second answer is
    scored with the first question still in front of the model."""
    await batch.execute(_plan(setup, repeat=2))
    assert len(FakeRuntime.turns) == 8
    assert all(len(turn) == 1 for turn in FakeRuntime.turns)


async def test_the_header_names_the_sources_each_variant_attached(
    setup: dict[str, Any],
) -> None:
    planned = _plan(setup)
    manifest = await batch.execute(planned)
    directory = setup["root"] / "runs" / planned.run_id
    by_variant = {
        e["variant_id"]: _lines(directory / e["path"])[0]["sources"]
        for e in manifest["recordings"]
    }
    assert by_variant == {"small": [{"id": "notes", "kind": "files"}], "bare": []}


async def test_a_failed_run_is_marked_and_the_others_still_run(
    setup: dict[str, Any],
) -> None:
    _variants(
        setup["work"],
        """\
variants:
  - {id: broken, runtime: fake, options: {fail: true}}
  - {id: small, runtime: fake, options: {model: fake-small}}
""",
    )
    seen: list[str] = []
    planned = _plan(setup)
    manifest = await batch.execute(planned, progress=lambda r: seen.append(r.status))
    statuses = {e["variant_id"]: e["status"] for e in manifest["recordings"]}
    assert statuses == {"broken": "failed", "small": "ok"}
    assert sorted(seen) == ["failed", "failed", "ok", "ok"]


async def test_a_source_that_fails_to_attach_fails_the_run(
    setup: dict[str, Any], tmp_path: Path
) -> None:
    """A missing source is a different variant, not a smaller one."""
    (tmp_path / "work" / "plain.yaml").write_text(
        "name: plain\nsources:\n"
        f"  - {{id: gone, kind: files, target: {tmp_path}/nope}}\n",
        encoding="utf-8",
    )
    planned = _plan(setup)
    manifest = await batch.execute(planned)
    directory = setup["root"] / "runs" / planned.run_id
    for entry in manifest["recordings"]:
        if entry["variant_id"] != "bare":
            continue
        assert entry["status"] == "failed"
        lines = _lines(directory / entry["path"])
        assert [e["type"] for e in lines] == ["run_context", "agent_failed"]
        assert "gone" in lines[1]["message"]


async def test_the_manifest_lists_every_run_before_the_first_one_starts(
    setup: dict[str, Any],
) -> None:
    """Written up front and after each run, so an interrupted batch still says what
    exists and what never ran."""
    planned = _plan(setup)
    directory = setup["root"] / "runs" / planned.run_id
    pending: list[list[str]] = []

    def look(_: batch.Recording) -> None:
        manifest = json.loads((directory / "manifest.json").read_text())
        pending.append([e["status"] for e in manifest["recordings"]])

    await batch.execute(planned, progress=look)
    assert pending[0] == ["ok", "pending", "pending", "pending"]
    assert pending[-1] == ["ok"] * 4


def test_planning_writes_nothing(setup: dict[str, Any]) -> None:
    planned = _plan(setup, repeat=3)
    assert planned.count == 12
    assert not setup["root"].exists()


def test_a_variant_is_paid_unless_its_host_is_local(setup: dict[str, Any]) -> None:
    _variants(
        setup["work"],
        """\
variants:
  - {id: home, runtime: fake, options: {host: local}}
  - {id: cloud, runtime: fake, options: {host: anthropic}}
  - {id: unstated, runtime: fake}
""",
    )
    assert {v.id: v.paid for v in _plan(setup).variants} == {
        "home": False,
        "cloud": True,
        "unstated": True,
    }


def test_the_built_in_deployment_refuses_to_write_into_the_package(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "latent")
    _variants(setup["work"], "variants:\n  - {id: small, runtime: fake}\n")
    with pytest.raises(batch.BatchError, match="intel deployment use"):
        _plan(setup)


def test_a_root_that_could_not_be_honoured_refuses_and_says_why(
    setup: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVAL_BUCKET_UNSET", raising=False)
    (tmp_path / "projects" / "deploy.yaml").write_text(
        "name: deploy\nroot: ${EVAL_BUCKET_UNSET}/runs\n", encoding="utf-8"
    )
    with pytest.raises(batch.BatchError, match="EVAL_BUCKET_UNSET"):
        _plan(setup)


def test_no_active_deployment_refuses(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LATENT_INTEL_PROJECT")
    with pytest.raises(batch.BatchError, match="intel deployment use"):
        _plan(setup)


@pytest.mark.parametrize(
    ("cases", "message"),
    [
        ("cases:\n  - {id: q1, input: {prompt: hi}}\n", "question"),
        ("cases:\n  - {id: q1, input: hi}\n", "question"),
        ("cases:\n  - {id: q1}\n  - {id: q2, input: []}\n", "q1, q2"),
        ("cases:\n  - {id: q1, input: {question: hi}}\n  - {id: q1}\n", "twice"),
        ("cases:\n  - {id: a/b}\n", "a/b"),
        ("cases:\n  - {id: .hidden}\n", ".hidden"),
        ("cases:\n  - {input: {question: hi}}\n", "id"),
        ("cases: []\n", "no cases"),
        ("- just a list\n", "cases:"),
        ("cases:\n  - {id: q1, metadata: [a]}\n", "metadata"),
        ("format: other\nformat_version: 1\ncases: []\n", "other"),
        ("format: latent-eval.dataset\nformat_version: 2\ncases: []\n", "v1"),
        ("format: latent-eval.dataset\nformat_version: true\ncases: []\n", "v1"),
        ("format: latent-eval.dataset\ncases: []\n", "None"),
    ],
)
def test_a_case_this_cannot_run_is_refused_before_anything_runs(
    setup: dict[str, Any], cases: str, message: str
) -> None:
    (setup["work"] / "cases.yaml").write_text(cases, encoding="utf-8")
    with pytest.raises(batch.BatchError, match=message):
        _plan(setup)


def test_cases_are_read_from_json_lines(setup: dict[str, Any]) -> None:
    path = setup["work"] / "cases.jsonl"
    path.write_text(
        '{"format": "latent-eval.dataset", "format_version": 1, "id": "q1", '
        '"input": {"question": "hi"}, "reference": "x"}\n\n'
        '{"format": "latent-eval.dataset", "format_version": 1, "id": "q2", '
        '"input": {"question": "there"}, "metadata": {"split": "dev"}}\n',
        encoding="utf-8",
    )
    planned = batch.plan(path, setup["work"] / "variants.yaml")
    assert planned.cases_stamped
    assert [(c.id, c.prompt, c.metadata) for c in planned.cases] == [
        ("q1", "hi", {}),
        ("q2", "there", {"split": "dev"}),
    ]


def test_an_unstamped_file_is_read_as_latent_eval_reads_it(
    setup: dict[str, Any],
) -> None:
    """The spec's own legacy sample: an extra key ignored, a numeric id a string."""
    path = setup["work"] / "legacy.jsonl"
    path.write_text(
        '{"id": "q1", "input": {"question": "Capital of France?"}, "notes": "x"}\n'
        '{"id": 2, "input": {"question": "Who wrote Hamlet?"}}\n',
        encoding="utf-8",
    )
    planned = batch.plan(path, setup["work"] / "variants.yaml")
    assert not planned.cases_stamped
    assert [c.id for c in planned.cases] == ["q1", "2"]


def test_an_unstamped_yaml_file_may_say_context_for_input(
    setup: dict[str, Any],
) -> None:
    (setup["work"] / "cases.yaml").write_text(
        "cases:\n  - {id: q1, context: {question: hi}}\n", encoding="utf-8"
    )
    [case] = _plan(setup).cases
    assert case.prompt == "hi"


def test_a_stamped_file_does_not_read_context(setup: dict[str, Any]) -> None:
    (setup["work"] / "cases.yaml").write_text(
        "format: latent-eval.dataset\nformat_version: 1\n"
        "cases:\n  - {id: q1, context: {question: hi}}\n",
        encoding="utf-8",
    )
    with pytest.raises(batch.BatchError, match="context"):
        _plan(setup)


def test_a_json_lines_file_that_mixes_stamps_is_refused(setup: dict[str, Any]) -> None:
    path = setup["work"] / "mixed.jsonl"
    path.write_text(
        '{"format": "latent-eval.dataset", "format_version": 1, "id": "q1", '
        '"input": {"question": "hi"}}\n'
        '{"id": "q2", "input": {"question": "there"}}\n',
        encoding="utf-8",
    )
    with pytest.raises(batch.BatchError, match="mixes"):
        batch.plan(path, setup["work"] / "variants.yaml")


@pytest.mark.parametrize(
    ("variants", "message"),
    [
        ("variants:\n  - {id: a, runtime: fake, modle: x}\n", "modle"),
        ("variants:\n  - {id: a}\n", "runtime"),
        (
            "variants:\n  - {id: a, runtime: fake}\n  - {id: a, runtime: fake}\n",
            "twice",
        ),
        ("variants:\n  - {id: a/b, runtime: fake}\n", "a/b"),
        ("variants:\n  - {id: a, runtime: nonesuch}\n", "nonesuch"),
        (
            "variants:\n  - {id: a, runtime: fake, options: {approval: auto}}\n",
            "approval",
        ),
        ("variants:\n  - {id: a, runtime: fake, deployment: missing}\n", "missing"),
        (
            "variants:\n  - {id: a, runtime: fake, deployment: ./gone.yaml}\n",
            "gone.yaml",
        ),
        ("variants: []\n", "no variants"),
        ("variants:\n  - {id: a, runtime: fake}\nextra: 1\n", "extra"),
    ],
)
def test_a_variant_this_cannot_run_is_refused_before_anything_runs(
    setup: dict[str, Any], variants: str, message: str
) -> None:
    _variants(setup["work"], variants)
    with pytest.raises(batch.BatchError, match=message):
        _plan(setup)


def test_a_variant_names_a_deployment_by_name(
    setup: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "projects" / "named.yaml").write_text("name: named\n", encoding="utf-8")
    _variants(
        setup["work"], "variants:\n  - {id: a, runtime: fake, deployment: named}\n"
    )
    [variant] = _plan(setup).variants
    assert variant.deployment_name == "named"


def test_repeat_must_be_at_least_one(setup: dict[str, Any]) -> None:
    with pytest.raises(batch.BatchError, match="repeat"):
        _plan(setup, repeat=0)


def test_a_run_id_sorts_by_time_and_is_a_safe_file_name(setup: dict[str, Any]) -> None:
    first, second = _plan(setup).run_id, _plan(setup).run_id
    assert first != second
    assert batch.SAFE_ID.fullmatch(first)
    assert first[:16] <= second[:16]


# -- from review ---------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".yaml", ".jsonl"])
def test_an_old_file_s_own_format_key_does_not_stamp_it(
    setup: dict[str, Any], suffix: str
) -> None:
    path = setup["work"] / f"own{suffix}"
    path.write_text(
        "format: qa\ncases:\n  - {id: q1, input: {question: hi}}\n"
        if suffix == ".yaml"
        else '{"format": "qa", "id": "q1", "input": {"question": "hi"}}\n',
        encoding="utf-8",
    )
    assert not batch.plan(path, setup["work"] / "variants.yaml").cases_stamped


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("cases:\n  - {id: q1, input: {question: hi}, refrence: x}\n", "refrence"),
        ("cases:\n  - {id: q1, input: hi}\n", "input must be an object"),
        ("cases:\n  - {id: 7, input: {question: hi}}\n", "non-empty string"),
        ("extra: 1\ncases:\n  - {id: q1, input: {question: hi}}\n", "extra"),
    ],
)
def test_a_stamped_file_is_held_to_the_spec_before_anything_is_spent(
    setup: dict[str, Any], body: str, message: str
) -> None:
    """latent-eval refuses these when it scores; refusing them only then is a paid
    batch thrown away."""
    (setup["work"] / "cases.yaml").write_text(
        "format: latent-eval.dataset\nformat_version: 1\n" + body, encoding="utf-8"
    )
    with pytest.raises(batch.BatchError, match=message):
        _plan(setup)


def test_case_ids_that_differ_only_in_case_are_refused(setup: dict[str, Any]) -> None:
    """On a case-insensitive filesystem they are one file, and the second run would
    overwrite the first while the manifest called both ok."""
    (setup["work"] / "cases.yaml").write_text(
        "cases:\n"
        "  - {id: Q1, input: {question: a}}\n"
        "  - {id: q1, input: {question: b}}\n",
        encoding="utf-8",
    )
    with pytest.raises(batch.BatchError, match="only in case"):
        _plan(setup)


@pytest.mark.parametrize(
    ("ids", "message"),
    [(("Small", "small"), "only in case"), (("manifest.json",), "manifest")],
)
def test_a_variant_id_that_would_collide_on_disk_is_refused(
    setup: dict[str, Any], ids: tuple[str, ...], message: str
) -> None:
    rows = "".join(f"  - {{id: {i}, runtime: fake}}\n" for i in ids)
    _variants(setup["work"], "variants:\n" + rows)
    with pytest.raises(batch.BatchError, match=message):
        _plan(setup)


class _Opening(FilesConnector):
    kind = "opening"

    async def aopen(self) -> None:
        raise RuntimeError("server crashed on start")


class _Closing(FilesConnector):
    kind = "closing"

    async def aclose(self) -> None:
        raise RuntimeError("would not close")


def _source_of_kind(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    kinds = {"files": FilesConnector, "opening": _Opening, "closing": _Closing}
    monkeypatch.setattr(connectors_base, "available_kinds", lambda: kinds)
    (setup["work"] / "plain.yaml").write_text(
        f"name: plain\nsources:\n  - {{id: s, kind: {kind}, target: {setup['tmp']}}}\n",
        encoding="utf-8",
    )


async def test_a_source_that_raises_on_attach_still_leaves_a_header(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third-party connector need not raise a `ConnectError`; whatever it raises,
    the recording opens with a header that places it."""
    _source_of_kind(setup, monkeypatch, "opening")
    planned = _plan(setup)
    manifest = await batch.execute(planned)
    directory = setup["root"] / "runs" / planned.run_id
    statuses = {e["variant_id"]: e["status"] for e in manifest["recordings"]}
    assert statuses == {"small": "ok", "bare": "failed"}
    validator = jsonschema.Draft202012Validator(SCHEMA)
    for entry in manifest["recordings"]:
        lines = _lines(directory / entry["path"])
        assert lines[0]["type"] == "run_context"
        assert (lines[0]["case_id"], lines[0]["variant_id"]) == (
            entry["case_id"],
            entry["variant_id"],
        )
        for line in lines:
            validator.validate(line)
        if entry["variant_id"] == "bare":
            assert [e["type"] for e in lines] == ["run_context", "agent_failed"]
            assert "crashed" in lines[1]["message"]
    assert manifest["finished"]


async def test_a_source_that_will_not_close_does_not_stop_the_batch(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _source_of_kind(setup, monkeypatch, "closing")
    manifest = await batch.execute(_plan(setup))
    assert [e["status"] for e in manifest["recordings"]] == ["ok"] * 4
    assert manifest["finished"]


def test_a_variant_whose_deployment_dropped_a_source_is_refused(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source skipped for an unset variable is only a problem to `project.load`;
    run anyway, the variant would be compared without it."""
    monkeypatch.delenv("EVAL_CORPUS_UNSET", raising=False)
    (setup["work"] / "plain.yaml").write_text(
        "name: plain\nsources:\n"
        "  - {id: notes, kind: files, target: ${EVAL_CORPUS_UNSET}/notes}\n",
        encoding="utf-8",
    )
    with pytest.raises(batch.BatchError, match="EVAL_CORPUS_UNSET"):
        _plan(setup)


def test_a_broken_active_deployment_says_why(setup: dict[str, Any]) -> None:
    (setup["tmp"] / "projects" / "deploy.yaml").write_text(
        "name: [unclosed\n", encoding="utf-8"
    )
    with pytest.raises(batch.BatchError, match="deploy.yaml"):
        _plan(setup)


async def test_a_runtime_that_cannot_run_here_plans_but_does_not_execute(
    setup: dict[str, Any],
) -> None:
    """A dry run on a machine without a variant's credentials still shows the matrix;
    only running refuses."""
    _variants(
        setup["work"],
        "variants:\n  - {id: small, runtime: fake, options: {unavailable: true}}\n",
    )
    planned = _plan(setup)
    [variant] = planned.variants
    assert variant.reason and "FAKE_KEY" in variant.reason
    with pytest.raises(batch.BatchError, match="FAKE_KEY"):
        await batch.execute(planned)
    assert not setup["root"].exists()
