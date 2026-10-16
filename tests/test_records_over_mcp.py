"""A records store through its own adapter: `latent-records serve`, reached like any
other MCP server.

latent-intel reads no records format itself. This is the one test against the real
server: it builds latent-records' own fixture deployment with that package's CLI, then
attaches the deployment's server. Skipped where latent-records is not installed (its
`serve` extra is `mcp`, which this package already has), which is every clone that does
not have it beside it.
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from latent_intel.connectors import base as connectors
from latent_intel.models import Capability, Ref
from latent_intel.session import Session
from tests.test_mcp import CallingRuntime, result_of

FIXTURE = Path(__file__).parent / "fixtures" / "records"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        importlib.util.find_spec("latent_records") is None,
        reason="latent-records is not installed",
    ),
]

RECORDS = [sys.executable, "-m", "latent_records.cli.main"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    """The fixture deployment, built: its store beneath it, with the test embedder."""
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    path = tmp_path / "deployments" / "brindlewick-hives"
    subprocess.run(
        [*RECORDS, "deployment", "run", str(path)], check=True, capture_output=True
    )
    return path


def records_serve(deployment: Path) -> str:
    """The server itself, started from its deployment, as a client records it."""
    return shlex.join([*RECORDS, "serve", "--deployment", str(deployment)])


async def test_a_records_store_is_an_mcp_source(deployment: Path) -> None:
    connector = await connectors.open_connector(
        connectors.build("mcp", "hives", records_serve(deployment))
    )
    try:
        tools = {t.name: t for t in connector.tools()}
        assert set(tools) == {"describe", "search", "contains", "get"}
        assert not any(t.effect.writes for t in tools.values())
        assert all(t.effect_declared for t in tools.values())
        answer = await connector.call(
            "search", {"query": "frame card", "space": "hivewright", "limit": 3}
        )
        # The server names keys by its store id and knows nothing of this client's
        # label; the `hives:` a citation carries is swapped in on this side.
        assert "brindlewick-hives:comb-ledger-a::chunk::" in answer
        assert Ref.find_all(answer, ["hives"]) == []
        assert connector.ref_prefix() == "brindlewick-hives"
        # Search and fetch with no model: the server lists its records as resources.
        described = connector.describe()
        assert {Capability.SEARCH, Capability.FETCH} <= set(described.capabilities)
        hits = await connector.search("comb")
        assert "hives:comb-ledger-a" in [h.ref for h in hits]
        record = await connector.fetch("comb-ledger-a")
        assert record.ref == "hives:comb-ledger-a"
        assert "frame card" in record.body.lower()
    finally:
        await connector.aclose()


async def test_a_records_store_s_refs_are_labelled_here(deployment: Path) -> None:
    """Agnostic both ways: the server's text names no client, and the recorded refs
    carry the label this session attached it under."""
    runtime = CallingRuntime("hives", "contains", {"phrase": "frame card"})
    session = Session(runtime=runtime)
    await session.connect(records_serve(deployment), kind="mcp", source_id="hives")
    result = await result_of(session)
    assert "hives:" not in result.output.replace("brindlewick-hives:", "")
    assert result.refs == [
        "hives:comb-ledger-a",
        "hives:comb-ledger-a::chunk::0001",
        "hives:comb-ledger-a::chunk::0002",
        "hives:comb-ledger-a::figure::fig-001",
        "hives:comb-ledger-a::table::table-001",
    ]


async def test_two_deployments_are_two_sources(
    deployment: Path, tmp_path: Path
) -> None:
    """Each deployment gets its own server from the same command; their keys never
    meet, because each server names them under its own store id."""
    other = tmp_path / "deployments" / "brindlewick-annex"
    other.mkdir()
    (other / "brindlewick-annex.yaml").write_text(
        (deployment / "brindlewick-hives.yaml")
        .read_text()
        .replace("store_id: brindlewick-hives", "store_id: brindlewick-annex")
    )
    subprocess.run(
        [*RECORDS, "deployment", "run", str(other)], check=True, capture_output=True
    )
    session = Session(runtime=CallingRuntime("annex", "get", {"id": "comb-ledger-a"}))
    await session.connect(records_serve(deployment), kind="mcp", source_id="hives")
    await session.connect(records_serve(other), kind="mcp", source_id="annex")
    prefixes = {k: c.ref_prefix() for k, c in session._connectors.items()}
    assert prefixes == {"hives": "brindlewick-hives", "annex": "brindlewick-annex"}
    result = await result_of(session)  # closes both through Session.aclose
    assert '"brindlewick-annex:comb-ledger-a"' in result.output
    assert "brindlewick-hives:" not in result.output
    assert result.refs == ["annex:comb-ledger-a"]
