"""The vendor-neutral half of an in-process turn.

Nothing here touches a vendor SDK, which is the point: if these needed `anthropic`
installed they would be testing `anthropic.py` instead, and the boundary the module
exists to draw would be untested.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from latent_intel import events as ev
from latent_intel.agent import turn
from latent_intel.models import Descriptor, Effect, ToolSpec


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def spec(name: str, effect: Effect = Effect.EXTERNAL_READ, **fields: Any) -> ToolSpec:
    return ToolSpec(name=name, source_id="design", effect=effect, **fields)


# -- what may be offered ----------------------------------------------------


@pytest.mark.parametrize(
    "effect", [Effect.LOCAL_WRITE, Effect.EXTERNAL_WRITE, Effect.DESTRUCTIVE]
)
def test_every_writing_effect_is_withheld_unless_approval_is_auto(
    effect: Effect,
) -> None:
    """There is no one to prompt inside a stream, so the offer is the gate.

    Parametrised over all three writing effects because omitting `DESTRUCTIVE` would
    admit the worst tools under the cautious setting — the trap `claude_cli` already
    fell into once.
    """
    tools = [spec("read"), spec("write", effect)]
    assert [t.name for t in turn.offered(tools, "ask")] == ["read"]
    assert [t.name for t in turn.offered(tools, "never")] == ["read"]
    assert len(turn.offered(tools, "auto")) == 2


def test_a_reading_tool_is_offered_under_every_setting() -> None:
    for approval in ("ask", "never", "auto"):
        assert turn.offered([spec("read", Effect.NONE)], approval)


# -- the wire name ----------------------------------------------------------


def test_the_router_namespace_is_folded_to_what_a_wire_format_accepts() -> None:
    """`source_id.name` is the router's namespace and the dot is not a legal character
    in a tool name — a call with one is refused by the API, not by us."""
    assert turn.wire_name(spec("wiki_get")) == "design_wiki_get"
    odd = ToolSpec(name="get page", source_id="claude.ai Drive")
    assert turn.wire_name(odd) == "claude_ai_Drive_get_page"


def test_a_very_long_qualified_name_is_cut_to_the_limit() -> None:
    """64 characters is the cap. The name is not invertible anyway, so the caller keeps
    a lookup rather than trying to split it back apart."""
    long = ToolSpec(name="x" * 80, source_id="store")
    assert len(turn.wire_name(long)) == 64


# -- the input schema -------------------------------------------------------


# -- what goes out, and what may not ----------------------------------------


def test_the_offered_tools_come_back_paired_with_the_names_they_go_out_under() -> None:
    """One composition of `offered` and `wire_name`, so both runtimes gate and fold
    identically rather than each doing half of it."""
    tools = [
        ToolSpec(name="search", source_id="my wiki", effect=Effect.EXTERNAL_READ),
        ToolSpec(name="get", source_id="files", effect=Effect.EXTERNAL_READ),
    ]
    assert turn.wired(tools, "ask") == [
        ("my_wiki_search", tools[0]),
        ("files_get", tools[1]),
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("my wiki", "search"), ("my_wiki", "search")),
        (("my", "wiki_search"), ("my_wiki", "search")),
    ],
)
def test_two_tools_that_fold_to_one_wire_name_are_refused_by_name(
    first: tuple[str, str], second: tuple[str, str]
) -> None:
    """Folding is not invertible, so the duplicate would reach the API as a 400 and the
    runtime's own lookup would route every call to whichever spec folded last. Both
    qualified names are in the message because neither one alone says what to change."""
    tools = [
        ToolSpec(name=first[1], source_id=first[0], effect=Effect.EXTERNAL_READ),
        ToolSpec(name=second[1], source_id=second[0], effect=Effect.EXTERNAL_READ),
    ]
    with pytest.raises(turn.ToolNameCollision) as caught:
        turn.wired(tools, "ask")
    message = str(caught.value)
    assert tools[0].qualified in message and tools[1].qualified in message
    assert "my_wiki_search" in message
    assert "different id" in message


def test_a_tool_withheld_by_the_gate_cannot_collide_with_one_that_is_offered() -> None:
    """The gate runs first, so a write that is never offered is not a name in use."""
    tools = [
        ToolSpec(name="search", source_id="my wiki", effect=Effect.LOCAL_WRITE),
        ToolSpec(name="search", source_id="my_wiki", effect=Effect.EXTERNAL_READ),
    ]
    assert [name for name, _ in turn.wired(tools, "ask")] == ["my_wiki_search"]


def test_a_tool_that_declares_no_parameters_still_gets_an_object_schema() -> None:
    """`ToolSpec.input_schema` defaults to `{}`, which is a legal Python default and an
    illegal tool definition."""
    assert turn.input_schema(spec("ping")) == {"type": "object", "properties": {}}


def test_a_declared_schema_is_passed_through_untouched() -> None:
    declared = {"type": "object", "properties": {"query": {"type": "string"}}}
    assert turn.input_schema(spec("search", input_schema=declared)) == declared


# -- the system prompt ------------------------------------------------------


def test_the_prompt_names_the_sources_and_asks_for_citations() -> None:
    prompt = turn.system_prompt([Descriptor(id="design", kind="wiki")])
    assert "design (wiki)" in prompt
    assert "`source:key`" in prompt


def test_no_sources_means_no_prompt_rather_than_an_empty_preamble() -> None:
    assert turn.system_prompt([]) == ""


# -- dispatching one call ---------------------------------------------------


async def _router(source_id: str, name: str, arguments: dict[str, Any]) -> str:
    return f"{source_id}:{name}:{sorted(arguments)}"


@pytest.mark.anyio
async def test_a_call_pairs_and_nests_under_its_own_start() -> None:
    """A renderer nests the two by `parent_id`, never by guessing from timing."""
    emitter = ev.Emitter(uuid4())
    started, result = await turn.dispatch(
        _router,
        spec("wiki_get"),
        source_id="design",
        name="wiki_get",
        arguments={"key": "gist"},
        emitter=emitter,
    )
    assert isinstance(started, ev.ToolStarted) and isinstance(result, ev.ToolResult)
    assert result.parent_id == started.event_id
    assert result.ok and "design:wiki_get" in result.output
    assert result.duration_ms is not None
    assert started.effect == str(Effect.EXTERNAL_READ)  # declared, never guessed


@pytest.mark.anyio
async def test_a_connector_that_raises_becomes_a_result_not_a_crash() -> None:
    """One bad call must not end the turn: the model has to be told what happened in
    order to do anything else."""

    async def angry(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the store is unreachable")

    _, result = await turn.dispatch(
        angry,
        spec("wiki_get"),
        source_id="design",
        name="wiki_get",
        arguments={},
        emitter=ev.Emitter(uuid4()),
    )
    assert not result.ok
    assert result.error == "the store is unreachable"
    assert result.output == ""


@pytest.mark.anyio
async def test_a_tool_the_model_invented_is_never_routed() -> None:
    """Unknown means unknown — calling the router with it would ask a connector to
    interpret a name this session never offered."""
    called: list[str] = []

    async def record(source_id: str, name: str, arguments: dict[str, Any]) -> str:
        called.append(name)
        return "never"

    _, result = await turn.dispatch(
        record,
        None,
        source_id="",
        name="delete_everything",
        arguments={},
        emitter=ev.Emitter(uuid4()),
    )
    assert called == []
    assert not result.ok and "delete_everything" in result.error


@pytest.mark.anyio
async def test_an_undeclared_tool_is_reported_at_the_unsafe_end() -> None:
    """`Effect` is declared, never inferred — and an absent declaration reads as a
    write, so a renderer shows it as one."""
    started, _ = await turn.dispatch(
        _router,
        None,
        source_id="",
        name="mystery",
        arguments={},
        emitter=ev.Emitter(uuid4()),
    )
    assert started.effect == str(Effect.EXTERNAL_WRITE)


@pytest.mark.anyio
async def test_a_runtime_with_no_router_says_so_rather_than_hanging() -> None:
    _, result = await turn.dispatch(
        None,
        spec("wiki_get"),
        source_id="design",
        name="wiki_get",
        arguments={},
        emitter=ev.Emitter(uuid4()),
    )
    assert not result.ok and "router" in result.error


# -- usage across rounds ----------------------------------------------------


class Usage:
    """What the SDK hands back — attributes, some of them None."""

    def __init__(self, **fields: int | None) -> None:
        self.input_tokens = fields.get("input_tokens")
        self.output_tokens = fields.get("output_tokens")
        self.cache_creation_input_tokens = fields.get("cache_creation_input_tokens")
        self.cache_read_input_tokens = fields.get("cache_read_input_tokens")


def test_usage_sums_every_round_rather_than_reporting_the_last() -> None:
    """A tool-heavy question is several round-trips, and the last one's usage is a
    small fraction of what it cost."""
    totals = turn.UsageTotals()
    totals.add(Usage(input_tokens=10, output_tokens=2))
    totals.add(Usage(input_tokens=40, output_tokens=6, cache_read_input_tokens=100))

    assert totals.totals(duration_ms=900) == {
        "input_tokens": 50,
        "output_tokens": 8,
        "cache_read_input_tokens": 100,
        "duration_ms": 900,
        "num_turns": 2,
    }


def test_a_field_that_is_none_is_absent_rather_than_zero() -> None:
    """Cache fields are None when caching was not in play; a zero would claim it was."""
    totals = turn.UsageTotals()
    totals.add(Usage(input_tokens=3, output_tokens=1))
    assert "cache_read_input_tokens" not in totals.totals(duration_ms=1)


def test_named_counts_are_summed_the_same_way_an_sdk_object_is() -> None:
    """A protocol that names its usage fields differently translates them and calls
    this, so the two paths must not drift into two accumulators."""
    totals = turn.UsageTotals()
    totals.add_counts(input_tokens=10, output_tokens=2)
    totals.add_counts(input_tokens=40, output_tokens=6, cache_read_input_tokens=100)

    assert totals.totals(duration_ms=900) == {
        "input_tokens": 50,
        "output_tokens": 8,
        "cache_read_input_tokens": 100,
        "duration_ms": 900,
        "num_turns": 2,
    }


def test_a_named_count_that_is_none_is_absent_rather_than_zero() -> None:
    """The caller passes every field it knows about, whether the response carried one
    or not; a zero would claim caching was in play when it was not."""
    totals = turn.UsageTotals()
    totals.add_counts(input_tokens=3, output_tokens=1, cache_read_input_tokens=None)
    assert "cache_read_input_tokens" not in totals.totals(duration_ms=1)


def test_usage_with_nothing_recorded_still_reports_the_turn() -> None:
    assert turn.UsageTotals().totals(duration_ms=5) == {
        "duration_ms": 5,
        "num_turns": 0,
    }
