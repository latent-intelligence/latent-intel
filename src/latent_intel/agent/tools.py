"""Tools that belong to the engine rather than to a source.

`Session.tools()` used to union connector tools and nothing else, so a tool about the
deployment itself had nowhere to live. This is that place, kept small: a connector
answers *where context comes from*, and loading a skill is not that.

**One tool so far: `load_skill`.** Each skill the model may use is listed in the system
prompt by name and description only, and its body arrives when the model asks for it —
progressive disclosure, as Claude Code, the Agent SDK and MCP-served skills all do it.
A skill marked `disable-model-invocation` is neither listed nor loadable.

**In-process only.** `Session.mcp_servers()` serves connectors, not this, so
`claude-cli` never sees an engine tool and is given the skills whole instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models import CapabilityError, Effect, SessionError, Skill, ToolSpec

#: The source id engine tools are qualified by. Reserved: a source that took it would
#: have its tools shadowed by ours, or ours by its.
ENGINE_ID = "engine"
LOAD_SKILL = "load_skill"


def loadable(skills: Sequence[Skill]) -> list[Skill]:
    """The skills a model may load — all but those marked `disable-model-invocation`."""
    return [skill for skill in skills if skill.model_invocable]


def specs(skills: Sequence[Skill]) -> list[ToolSpec]:
    """The engine's tools for this deployment. Empty when there is nothing to load.

    `Effect.NONE`: it reads text the engine already holds, so it is offered under every
    approval mode. The names are an `enum`, so a model cannot ask for one that is not
    there.
    """
    names = [skill.name for skill in loadable(skills)]
    if not names:
        return []
    return [
        ToolSpec(
            name=LOAD_SKILL,
            source_id=ENGINE_ID,
            description=(
                "Load one of this deployment's skills in full, by name. The system "
                "prompt lists each with what it covers; load a skill before answering "
                "a question it covers."
            ),
            effect=Effect.NONE,
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "enum": names}},
                "required": ["name"],
            },
        )
    ]


async def call(skills: Sequence[Skill], name: str, arguments: dict[str, Any]) -> str:
    """Run one engine tool. Raises, as a connector's `call` does; the runtime turns
    that into a failed `ToolResult` the model is told about."""
    if name != LOAD_SKILL:
        raise CapabilityError(f"the engine offers no tool '{name}'")
    wanted = str(arguments.get("name") or "")
    choices = loadable(skills)
    for skill in choices:
        if skill.name == wanted:
            return skill.body
    names = ", ".join(skill.name for skill in choices) or "none"
    raise SessionError(f"no skill named '{wanted}' — loadable: {names}")


__all__ = ["ENGINE_ID", "LOAD_SKILL", "call", "loadable", "specs"]
