"""A wiki through its own adapter: `lw serve`, reached like any other MCP server.

latent-intel reads no wiki format itself. This is the one test against the real server,
over the copied format fixtures, and it is skipped where latent-wiki is not installed —
which is every clone that does not have it beside it.
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest

from latent_intel.connectors import base as connectors
from latent_intel.models import Capability, Effect, Ref

FORMATS = Path(__file__).parent / "fixtures" / "formats"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        importlib.util.find_spec("latent_wiki") is None,
        reason="latent-wiki is not installed",
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def lw_serve() -> str:
    """The server itself, not a wrapper: the interpreter running these tests."""
    argv = [sys.executable, "-m", "latent_wiki.cli.main", "serve"]
    argv += ["--store", str(FORMATS / "wiki-store")]
    argv += ["--context", str(FORMATS / "context-store")]
    argv += ["--id", "heron"]  # the source id, so refs read `heron:<key>`
    return shlex.join(argv)


async def test_a_wiki_is_an_mcp_source_pointing_at_lw_serve() -> None:
    connector = await connectors.open_connector(
        connectors.build("mcp", "heron", lw_serve())
    )
    try:
        tools = {t.name: t for t in connector.tools()}
        assert {"wiki_overview", "wiki_search", "wiki_get", "wiki_view"} <= set(tools)
        assert all(t.effect is Effect.EXTERNAL_READ for t in tools.values())
        assert all(t.effect_declared for t in tools.values())
        assert tools["wiki_search"].input_schema["required"] == ["query"]
        answer = await connector.call("wiki_search", {"query": "limpets"})
        assert "cut:" in answer
        # Refs in tool output are this client's refs, so citations resolve.
        assert "heron:count-limpets" in Ref.find_all(answer, ["heron"])
        described = connector.describe()
        assert described.detail["description"].startswith("Heronsgate")
        # Search and get with no model: the server lists its pages as resources.
        assert Capability.SEARCH in described.capabilities
        hits = await connector.search("limpets")
        assert "heron:count-limpets" in [h.ref for h in hits]
        page = await connector.fetch("count-limpets")
        assert page.body.startswith("Count limpets")
    finally:
        await connector.aclose()
