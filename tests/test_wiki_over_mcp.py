"""A wiki through its own adapter, `latent-wiki serve`, reached like any MCP server.

latent-intel reads no wiki format itself. This is the one test against the real server,
over a deployment built around the copied format fixtures, and it is skipped where
latent-wiki is not installed — which is every clone that does not have it beside it.
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from latent_intel.connectors import base as connectors
from latent_intel.models import Capability, Effect, Ref
from latent_intel.session import Session
from tests.test_mcp import CallingRuntime, result_of

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


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    """A deployment around the format fixtures: declaration, config, wiki, context."""
    root = tmp_path / "heronsgate"
    shutil.copytree(FORMATS / "wiki-store", root / "wiki")
    shutil.copytree(FORMATS / "context-store", root / "context")
    shutil.copy(root / "wiki" / "wiki.yaml", root / "wiki.yaml")
    (root / "heronsgate.yaml").write_text(
        "schema_version: 1\nwiki: {config: wiki.yaml}\n"
    )
    return root


def latent_wiki_serve(deployment: Path) -> str:
    """The server itself, not a wrapper: the interpreter running these tests."""
    argv = [sys.executable, "-m", "latent_wiki.cli.main", "serve"]
    argv += ["--deployment", str(deployment)]
    return shlex.join(argv)


async def test_a_wiki_is_an_mcp_source_pointing_at_latent_wiki_serve(
    deployment: Path,
) -> None:
    connector = await connectors.open_connector(
        connectors.build("mcp", "heron", latent_wiki_serve(deployment))
    )
    try:
        tools = {t.name: t for t in connector.tools()}
        assert {"wiki_overview", "wiki_search", "wiki_get", "wiki_view"} <= set(tools)
        assert all(t.effect is Effect.EXTERNAL_READ for t in tools.values())
        assert all(t.effect_declared for t in tools.values())
        assert tools["wiki_search"].input_schema["required"] == ["query"]
        answer = await connector.call("wiki_search", {"query": "limpets"})
        assert "cut:" in answer
        # The server names pages by its store id and knows nothing of this client's
        # label; the `heron:` a citation carries is swapped in on this side.
        assert "heronsgate:count-limpets" in answer
        assert Ref.find_all(answer, ["heron"]) == []
        described = connector.describe()
        assert described.detail["description"].startswith("Heronsgate")
        # Search and get with no model: the server lists its pages as resources.
        assert Capability.SEARCH in described.capabilities
        hits = await connector.search("limpets")
        assert "heron:count-limpets" in [h.ref for h in hits]
        page = await connector.fetch("count-limpets")
        assert page.body.startswith("---\n")  # the page as written, not a rendering
        assert "Count limpets" in page.body
    finally:
        await connector.aclose()


async def test_a_wiki_s_refs_are_labelled_here_and_never_by_the_server(
    deployment: Path,
) -> None:
    """Agnostic both ways: the server's text names no client, and the recorded refs
    carry the label this session attached it under."""
    runtime = CallingRuntime("heron", "wiki_search", {"query": "limpets"})
    session = Session(runtime=runtime)
    await session.connect(latent_wiki_serve(deployment), kind="mcp", source_id="heron")
    result = await result_of(session)
    assert "heron:" not in result.output
    assert result.refs == ["heron:count-limpets"]
