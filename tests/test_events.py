"""The event contract.

These are the tests that protect the web client, which does not exist yet. Every one of
them is checking something that works fine in-process and fails only once events cross a
socket — which is why they have to be asserted now rather than discovered later.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from latent_intel import events as ev
from latent_intel.models import AttachedSource, Fingerprint, Hit, Provenance

STREAMS = Path(__file__).parent / "fixtures" / "streams"
FIXTURES = sorted(STREAMS.glob("*.jsonl"))


def test_fixtures_exist() -> None:
    """Guards the guard: an empty fixture directory would make every replay test below
    pass by iterating nothing."""
    assert {p.stem for p in FIXTURES} == {
        "agent_turn",
        "approval",
        "failure",
        "retrieval",
        "search",
        "tool_call",
    }


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_recorded_stream_parses_and_round_trips(path: Path) -> None:
    """Every event survives JSON with its envelope intact.

    This is the web client's only guarantee. If a field cannot make the trip, a renderer
    reading SSE sees something different from a renderer reading the same event
    in-process, and the frontends stop being interchangeable.
    """
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert lines

    for line in lines:
        event = ev.parse_event(line)
        assert not isinstance(event, ev.UnknownEvent), f"{path.name}: {event.type}"
        assert ev.parse_event(ev.dump_event(event)) == event
        # The envelope specifically — it is the part with no other test.
        payload = json.loads(ev.dump_event(event))
        for key in (
            "schema_version",
            "event_id",
            "session_id",
            "operation_id",
            "sequence",
            "ts",
            "type",
        ):
            assert key in payload, f"{path.name}: envelope missing {key}"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_sequence_is_monotonic_within_an_operation(path: Path) -> None:
    """Ordering has to survive a transport that does not preserve it."""
    events = list(ev.read_stream(path.read_text()))
    by_operation: dict[str, list[int]] = {}
    for event in events:
        by_operation.setdefault(str(event.operation_id), []).append(event.sequence)
    for sequences in by_operation.values():
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_nested_events_name_a_parent_in_the_same_stream(path: Path) -> None:
    events = list(ev.read_stream(path.read_text()))
    ids = {e.event_id for e in events}
    for event in events:
        if event.parent_id is not None:
            assert event.parent_id in ids, f"{path.name}: dangling parent"


def test_every_result_carries_provenance() -> None:
    """A result that cannot say where it came from is worse than no result — a reader
    has no signal to doubt it. Required field, not a convention."""
    for path in FIXTURES:
        for event in ev.read_stream(path.read_text()):
            if isinstance(event, ev.RetrievalResult):
                for hit in event.hits:
                    assert hit.provenance.source_id == event.source_id
            if isinstance(event, ev.DocumentFetched):
                assert event.doc.provenance.source_id == event.doc.source_id


def test_unknown_type_is_preserved_not_raised() -> None:
    """A newer producer must never break an older renderer."""
    payload = {
        "type": "quantum_entanglement_started",
        "schema_version": 99,
        "session_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "sequence": 7,
        "novel_field": "kept",
    }
    event = ev.parse_event(payload)
    assert isinstance(event, ev.UnknownEvent)
    assert event.type == "quantum_entanglement_started"
    assert event.sequence == 7
    assert event.payload["novel_field"] == "kept"


def test_malformed_input_does_not_raise() -> None:
    """A stream is a thing you are in the middle of. Failing hard halfway through loses
    everything already received."""
    for bad in ("not json at all", "[1, 2, 3]", '{"type": "user_message"}'):
        assert isinstance(ev.parse_event(bad), ev.UnknownEvent)


def test_known_types_matches_the_union() -> None:
    """A new event class added to the union but not exported, or vice versa, is the kind
    of drift that only shows up as a renderer silently ignoring something."""
    assert len(ev.KNOWN_TYPES) == 18
    assert "retrieval_result" in ev.KNOWN_TYPES
    assert "unknown" not in ev.KNOWN_TYPES  # the fallback is not a member


def test_emitter_stamps_a_coherent_envelope() -> None:
    session = uuid4()
    emitter = ev.Emitter(session)

    first = emitter.emit(ev.UserMessage, text="hello")
    started = emitter.emit(
        ev.ToolStarted, tool="design.wiki_search", source_id="design"
    )
    nested = emitter.nested(started)
    result = nested.emit(ev.ToolResult, tool="design.wiki_search", source_id="design")

    assert first.session_id == session
    assert first.operation_id == started.operation_id == result.operation_id
    assert [first.sequence, started.sequence, result.sequence] == [1, 2, 3]
    assert result.parent_id == started.event_id
    assert first.parent_id is None


def test_a_completed_turn_s_cost_round_trips_and_is_optional() -> None:
    """`cost_usd` is a float, which is why it is not in `usage`. Additive: an event
    written before it existed still parses, with no cost rather than a zero."""
    emitter = ev.Emitter(uuid4())
    done = emitter.emit(
        ev.AgentCompleted, text="a", usage={"num_turns": 1}, cost_usd=0.5
    )
    assert ev.parse_event(ev.dump_event(done)) == done

    older = json.loads(ev.dump_event(done))
    del older["cost_usd"]
    parsed = ev.parse_event(json.dumps(older))
    assert isinstance(parsed, ev.AgentCompleted) and parsed.cost_usd is None


def test_emitter_gives_each_operation_its_own_id() -> None:
    session = uuid4()
    assert ev.Emitter(session).operation_id != ev.Emitter(session).operation_id


def test_scores_are_not_comparable_across_sources() -> None:
    """The search fixture pins this deliberately: a lexical count of 12.0 and a cosine
    of 0.81 in the same stream. Any future 'merge and sort' would rank the cosine last
    on a number that does not mean the same thing — so results group by source."""
    events = list(ev.read_stream((STREAMS / "search.jsonl").read_text()))
    results = [e for e in events if isinstance(e, ev.RetrievalResult)]
    assert len(results) == 2
    assert {r.source_id for r in results} == {"design", "papers"}
    assert max(h.score for h in results[0].hits) > 1.0
    assert max(h.score for h in results[1].hits) < 1.0


def test_hit_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        Hit(ref="a:b", source_id="a", title="t")  # type: ignore[call-arg]

    ok = Hit(
        ref="a:b",
        source_id="a",
        title="t",
        provenance=Provenance(source_id="a", method="search"),
    )
    assert ok.provenance.method == "search"


def test_a_user_message_s_skills_round_trip_and_an_older_one_still_parses() -> None:
    """`skills` is additive: recorded streams from before it read as none."""
    message = ev.Emitter(uuid4()).emit(ev.UserMessage, text="q", skills=["review"])
    assert ev.parse_event(message.model_dump_json()) == message
    older = message.model_dump(mode="json")
    del older["skills"]
    parsed = ev.parse_event(json.dumps(older))
    assert isinstance(parsed, ev.UserMessage) and parsed.skills == []


def test_a_run_context_round_trips_with_its_fingerprint() -> None:
    context = ev.Emitter(uuid4()).emit(
        ev.RunContext,
        purpose="eval",
        runtime="custom",
        model="m",
        context=Fingerprint(persona="a" * 64, skills={"s": "b" * 64}, hash="c" * 64),
        sources=[AttachedSource(id="design", kind="wiki")],
        case_id="q17",
        variant_id="haiku",
        epoch=2,
    )
    assert ev.parse_event(ev.dump_event(context)) == context
    assert context.format == "latent-intel.recording"


def test_tool_refs_are_additive() -> None:
    """A stream recorded before `refs` existed reads as a result with none."""
    result = ev.Emitter(uuid4()).emit(
        ev.ToolResult, tool="t", source_id="d", refs=["d:a", "d:b"]
    )
    assert ev.parse_event(result.model_dump_json()) == result
    older = result.model_dump(mode="json")
    del older["refs"]
    parsed = ev.parse_event(json.dumps(older))
    assert isinstance(parsed, ev.ToolResult) and parsed.refs == []


# -- the recording format ---------------------------------------------------

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "schemas" / f"recording.v{ev.SCHEMA_VERSION}.json"
RECORDINGS = sorted((ROOT / "tests/fixtures/formats/recording").glob("*.jsonl"))


def test_the_committed_schema_is_the_one_the_models_produce() -> None:
    """Regenerate with `just schemas` when an event changes."""
    assert json.loads(SCHEMA.read_text()) == ev.recording_schema()


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: p.stem)
def test_every_published_recording_conforms(path: Path) -> None:
    import jsonschema

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for line in lines:
        validator.validate(line)
    assert lines[0]["type"] == "run_context"
    assert lines[0]["format"] == ev.RECORDING_FORMAT
    assert lines[0]["format_version"] == ev.SCHEMA_VERSION
    assert not any(isinstance(ev.parse_event(line), ev.UnknownEvent) for line in lines)


def test_the_schema_rejects_a_malformed_header() -> None:
    import jsonschema

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    header = json.loads(RECORDINGS[0].read_text().splitlines()[0])
    header["sources"] = [{"id": "design"}]  # no kind
    assert not validator.is_valid(header)


def test_what_a_session_records_conforms() -> None:
    """The producer validates its own output, not only its hand-built samples."""
    import jsonschema

    from latent_intel.session import Session

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    validator.validate(json.loads(ev.dump_event(Session().run_context())))


def test_the_schema_holds_producers_to_the_known_purposes() -> None:
    """The model reads any purpose, so a newer one still parses as a header; the
    schema is where a producer is held to the list."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    header = json.loads(RECORDINGS[0].read_text().splitlines()[0])
    header["purpose"] = "benchmark"
    assert not validator.is_valid(header)
    assert isinstance(ev.parse_event(header), ev.RunContext)


def test_published_headers_are_ones_the_writer_produces() -> None:
    """A sample a consumer copies must not show a header this build cannot write."""
    from latent_intel.session import Session

    written = set(json.loads(ev.dump_event(Session().run_context())))
    for path in RECORDINGS:
        header = json.loads(path.read_text().splitlines()[0])
        assert set(header) == written
        assert header["context"]["hash"]
