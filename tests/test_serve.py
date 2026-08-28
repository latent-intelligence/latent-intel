"""The MCP server that ships with the client.

The rule under test: **a source this client can read, an agent can reach.** Agent
access used to depend on `shutil.which("lw")` — another package's console script, on
PATH — so a correctly configured store was invisible to the agent for a reason no
operator could see.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from latent_intel import serve
from latent_intel.connectors.files import FilesConnector
from latent_intel.models import ConnectError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "note.md").write_text("# Note\n\nCompaction is bounded.\n")
    (tmp_path / "other.md").write_text("# Other\n\nProvenance traces to a store.\n")
    return tmp_path


# -- the launcher ------------------------------------------------------------


def test_the_launcher_is_the_running_interpreter(corpus: Path) -> None:
    """Not a console script. `sys.executable` is the same environment by construction,
    so there is no PATH lookup to fail and no second package to install."""
    spec = serve.launch_spec("files", str(corpus), "notes", {})

    assert spec["command"] == sys.executable
    assert spec["args"][:2] == ["-m", "latent_intel.serve"]


def test_options_survive_the_process_boundary(corpus: Path) -> None:
    """A path and an optional pattern is the whole contract for a directory."""
    spec = serve.launch_spec("files", str(corpus), "notes", {"pattern": "**/*.txt"})
    args = spec["args"]

    assert args[args.index("--option") + 1] == "pattern=**/*.txt"
    assert args[args.index("--target") + 1] == str(corpus)
    assert args[args.index("--id") + 1] == "notes"


def test_a_uri_target_is_passed_through_untouched() -> None:
    """`s3://…` must not be normalised into a relative path — the bug this codebase has
    shipped repeatedly in other guises."""
    spec = serve.launch_spec("wiki", "s3://bucket/wikis/design", "design", {})
    args = spec["args"]

    assert args[args.index("--target") + 1] == "s3://bucket/wikis/design"


# -- schema, taken from the connector rather than re-derived -----------------


def test_required_and_optional_arguments_match_the_declared_schema() -> None:
    signature = serve._signature(
        {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        }
    )

    assert signature.parameters["query"].default is signature.empty
    assert signature.parameters["limit"].default == 0
    assert signature.parameters["limit"].annotation is int


def test_an_optional_string_defaults_to_empty_not_none() -> None:
    """Connectors read these with `str(...)`; a None crossing the boundary is a bug."""
    signature = serve._signature(
        {"type": "object", "properties": {"tag": {"type": "string"}}}
    )

    assert signature.parameters["tag"].default == ""


# -- the server ---------------------------------------------------------------


async def test_the_server_exposes_exactly_what_the_connector_declares(
    corpus: Path,
) -> None:
    connector = FilesConnector("notes", str(corpus))
    server = serve.build_server(connector)

    exposed = {tool.name for tool in await server.list_tools()}
    assert exposed == {spec.name for spec in connector.tools()}


async def test_read_only_tools_are_annotated_as_such(corpus: Path) -> None:
    """A client that gates on effects reads this. An absent annotation is correctly
    treated as "assume it writes", so declaring it is what keeps reads ungated."""
    server = serve.build_server(FilesConnector("notes", str(corpus)))

    for tool in await server.list_tools():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


async def test_a_tool_call_returns_what_the_connector_returns(corpus: Path) -> None:
    connector = FilesConnector("notes", str(corpus))
    server = serve.build_server(connector)

    result = await server.call_tool("files_search", {"query": "compaction"})

    assert "note.md" in str(result)


async def test_an_unknown_kind_names_what_is_installed() -> None:
    """Silence is the failure mode this whole module exists to remove, so the refusal
    has to say which kinds exist."""
    with pytest.raises(ConnectError, match="files, mcp, wiki"):
        await serve._run("nonexistent-kind", "/tmp", "x", {})
