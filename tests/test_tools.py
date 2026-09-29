"""The engine's own tools — `load_skill`, and the rules it enforces."""

from __future__ import annotations

import pytest

from latent_intel.agent import tools
from latent_intel.models import CapabilityError, Effect, SessionError, Skill

SKILLS = [
    Skill(name="citation-style", body="Cite inline.", description="How to cite."),
    Skill(name="deploy", body="Ship it.", description="d", model_invocable=False),
]


def test_load_skill_offers_only_what_the_model_may_load() -> None:
    """`Effect.NONE` so every approval mode offers it; the names are an enum, so a
    model cannot ask for one that is not there."""
    (spec,) = tools.specs(SKILLS)
    assert spec.qualified == "engine.load_skill"
    assert spec.effect == Effect.NONE
    assert spec.input_schema["properties"]["name"]["enum"] == ["citation-style"]


def test_no_loadable_skill_means_no_tool() -> None:
    assert tools.specs([]) == []
    assert tools.specs(SKILLS[1:]) == []


@pytest.mark.anyio
async def test_load_skill_returns_the_body() -> None:
    assert await tools.call(SKILLS, "load_skill", {"name": "citation-style"}) == (
        "Cite inline."
    )


@pytest.mark.anyio
async def test_a_name_it_may_not_load_fails_and_lists_what_it_may() -> None:
    for name in ("nope", "deploy"):
        with pytest.raises(SessionError, match="loadable: citation-style"):
            await tools.call(SKILLS, "load_skill", {"name": name})


@pytest.mark.anyio
async def test_an_unknown_engine_tool_is_a_capability_error() -> None:
    with pytest.raises(CapabilityError):
        await tools.call(SKILLS, "research", {})
