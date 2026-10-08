"""A deployment's skills over MCP — the Skills extension (SEP-2640), and its fallback.

SEP-2640 serves skills through the Resources primitive: each skill's `SKILL.md` is a
resource at `skill://<name>/SKILL.md`, and the server declares the
`io.modelcontextprotocol/skills` extension with two methods, `skills/list` (the index)
and `skills/get` (one entry by URI). The skills are the files a deployment already
keeps for Claude Code and this engine (`project._skills`, ADR-005), served unchanged.

**Kept in one file on purpose.** The SDK's extension API (SEP-2133) and client support
for SEP-2640 are both new; if either moves, this is the only file that does. A client
that does not negotiate the extension still reaches every skill through the
`load_skill` tool `serve_project` registers beside it.

Only `SKILL.md` is served — the same rule `project._skills` applies when it reads a
skill directory.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import TextResource
from mcp_types import RequestParams

from .models import Skill

IDENTIFIER = "io.modelcontextprotocol/skills"
SCHEME = "skill://"


def uri(skill: Skill) -> str:
    return f"{SCHEME}{skill.name}/SKILL.md"


class GetSkillParams(RequestParams):
    uri: str


class SkillsExtension(Extension):
    """Serves a deployment's model-invocable skills as `skill://` resources."""

    identifier = IDENTIFIER

    def __init__(self, skills: Sequence[Skill]) -> None:
        self._skills = [s for s in skills if s.model_invocable]

    def entries(self) -> list[dict[str, Any]]:
        """The `skills/list` index: one entry per skill, no bodies."""
        return [
            {
                "uri": uri(s),
                "name": s.name,
                "description": s.description,
                "mimeType": "text/markdown",
            }
            for s in self._skills
        ]

    def get(self, wanted: str) -> dict[str, Any]:
        """One skill by URI, body included. An unknown URI is an error, not empty."""
        for skill in self._skills:
            if uri(skill) == wanted:
                return {**self.entries()[self._skills.index(skill)], "text": skill.body}
        known = ", ".join(uri(s) for s in self._skills) or "none"
        raise ValueError(f"no skill at {wanted!r}; served: {known}")

    def resources(self) -> Sequence[ResourceBinding]:
        return [
            ResourceBinding(
                TextResource(
                    uri=uri(s),
                    name=s.name,
                    description=s.description,
                    mime_type="text/markdown",
                    text=s.body,
                )
            )
            for s in self._skills
        ]

    def methods(self) -> Sequence[MethodBinding]:
        async def list_skills(
            ctx: ServerRequestContext[Any, Any], params: RequestParams
        ) -> dict[str, Any]:
            return {"skills": self.entries()}

        async def get_skill(
            ctx: ServerRequestContext[Any, Any], params: GetSkillParams
        ) -> dict[str, Any]:
            return {"skill": self.get(params.uri)}

        return [
            MethodBinding("skills/list", RequestParams, list_skills),
            MethodBinding("skills/get", GetSkillParams, get_skill),
        ]
