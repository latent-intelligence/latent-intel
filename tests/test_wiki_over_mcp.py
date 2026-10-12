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
from latent_intel.models import Effect

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
        answer = await connector.call("wiki_search", {"query": "limpets"})
        assert "heronsgate:count-limpets" in answer
        assert "cut:" in answer
    finally:
        await connector.aclose()
