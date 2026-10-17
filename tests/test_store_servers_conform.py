"""Both store servers, `latent-wiki serve` and `latent-records serve`, held to the same
rules for an MCP adapter, so the two cannot drift apart.

The rules: a server starts from a deployment's folder and from nothing less; its MCP
name is the store's own id, and every key it prints is `<that name>:<key>`; no tool
writes; a search is bounded and says what it cut; every resource it lists reads back.
Each check runs over real stdio against the deployment that server's own over-MCP test
builds, and is skipped where that server's package is not installed.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, types
from mcp.client.stdio import StdioServerParameters, stdio_client

# Each server's own over-MCP test builds its deployment; these fixtures are that build.
from tests.test_records_over_mcp import deployment as records_deployment  # noqa: F401
from tests.test_records_over_mcp import records_serve
from tests.test_wiki_over_mcp import deployment as wiki_deployment  # noqa: F401
from tests.test_wiki_over_mcp import latent_wiki_serve

pytestmark = pytest.mark.anyio

Call = tuple[str, dict[str, Any]]

#: Words a tool that writes would carry in its name or an argument's.
WRITES = {
    "action", "add", "append", "create", "delete", "drop", "edit", "import", "insert",
    "publish", "put", "remove", "rename", "save", "set", "update", "upsert", "write",
}  # fmt: skip


@dataclass(frozen=True)
class Store:
    """One server, and the calls its own tool contract defines for each rule."""

    fixture: str
    store_id: str
    serve: Callable[[Path], str]
    lists_keys: Call  # an answer naming every listed resource
    past_bound: Call | None  # a search the contract refuses for its limit
    cut_short: tuple[str, dict[str, Any], str]  # a capped call, and what it says


WIKI = Store(
    fixture="wiki_deployment",
    store_id="heronsgate",
    serve=latent_wiki_serve,
    lists_keys=("wiki_index", {}),
    past_bound=None,
    cut_short=("wiki_search", {"query": "pool", "limit": 1}, "cut: first 1;"),
)
RECORDS = Store(
    fixture="records_deployment",
    store_id="brindlewick-hives",
    serve=records_serve,
    lists_keys=("contains", {"phrase": "the"}),
    past_bound=("search", {"query": "frame", "space": "hivewright", "limit": 500}),
    cut_short=("contains", {"phrase": "the"}, '"truncated": '),
)


def installed(package: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        importlib.util.find_spec(package.replace("-", "_")) is None,
        reason=f"{package} is not installed",
    )


@pytest.fixture(
    params=[
        pytest.param(WIKI, marks=installed("latent-wiki"), id="latent-wiki"),
        pytest.param(RECORDS, marks=installed("latent-records"), id="latent-records"),
    ]
)
def store(request: pytest.FixtureRequest) -> Store:
    return request.param  # type: ignore[no-any-return]


@pytest.fixture
def folder(store: Store, request: pytest.FixtureRequest) -> Path:
    return request.getfixturevalue(store.fixture)  # type: ignore[no-any-return]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def connected(store: Store, folder: Path) -> AsyncIterator[Client]:
    argv = shlex.split(store.serve(folder))
    server = StdioServerParameters(command=argv[0], args=argv[1:])
    with tempfile.TemporaryFile("w+") as errlog:
        async with Client(stdio_client(server, errlog=errlog)) as client:
            yield client


def text_of(result: types.CallToolResult) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, types.TextContent))


@pytest.mark.parametrize("how", ["a bare name", "no deployment"])
def test_it_starts_from_a_folder_and_refuses_less(
    store: Store, folder: Path, how: str
) -> None:
    argv = shlex.split(store.serve(folder))  # ends `--deployment <folder>`
    argv = [*argv[:-1], folder.name] if how == "a bare name" else argv[:-2]
    run = subprocess.run(
        argv,
        cwd=folder.parent,  # the name is refused even where a folder has it
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    said = (run.stdout + run.stderr).strip()
    assert run.returncode != 0
    assert len(said.splitlines()) == 1, said
    assert "Traceback" not in said
    assert "folder" in said, said


async def test_its_name_is_the_store_id_and_every_key_is_under_it(
    store: Store, folder: Path
) -> None:
    async with connected(store, folder) as client:
        name = client.server_info.name
        listed = (await client.list_resources()).resources
        answer = text_of(await client.call_tool(*store.lists_keys))
    assert name == store.store_id
    assert listed
    for key in (str(r.uri) for r in listed):
        assert f"{name}:{key}" in answer
        named_as = re.findall(rf"(?<![\w-])([\w-]+):{re.escape(key)}(?![\w-])", answer)
        assert set(named_as) == {name}, key


async def test_no_tool_writes(store: Store, folder: Path) -> None:
    async with connected(store, folder) as client:
        tools = (await client.list_tools()).tools
    assert tools
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert not tool.annotations.destructive_hint, tool.name
        named = " ".join([tool.name, *tool.input_schema.get("properties", {})])
        assert not set(re.split(r"[\W_]+", named.lower())) & WRITES, tool.name
        first = (tool.description or "").split(maxsplit=1)[:1]
        assert not {w.lower().strip(".,") for w in first} & WRITES, tool.name


async def test_a_search_past_its_bound_is_refused(store: Store, folder: Path) -> None:
    if store.past_bound is None:
        pytest.skip("its contract caps the limit without saying it refuses one")
    async with connected(store, folder) as client:
        result = await client.call_tool(*store.past_bound)
    said = text_of(result).strip()
    assert result.is_error
    assert len(said.splitlines()) == 1, said
    assert "limit" in said


async def test_a_capped_answer_says_what_it_cut(store: Store, folder: Path) -> None:
    tool, arguments, cut = store.cut_short
    async with connected(store, folder) as client:
        said = text_of(await client.call_tool(tool, arguments))
    assert cut in said


async def test_every_listed_resource_reads_back(store: Store, folder: Path) -> None:
    async with connected(store, folder) as client:
        listed = (await client.list_resources()).resources
        read = [(await client.read_resource(r.uri)).contents for r in listed]
    assert listed
    for resource, contents in zip(listed, read, strict=True):
        assert contents, resource.uri
        assert all(
            isinstance(c, types.TextResourceContents) and c.text.strip()
            for c in contents
        ), resource.uri
