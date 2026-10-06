"""The code connector — a repository read as text, by path and line span.

A temporary directory stands in for a repository. No graph, no network, no model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from latent_intel import serve
from latent_intel.connectors import base as connectors
from latent_intel.connectors.code import MAX_SEARCH_BYTES, CodeConnector
from latent_intel.models import Capability, ConnectError, Effect

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "ranking.py").write_text(
        "".join(f"line {n}\n" for n in range(1, 50))
        + "def rank_hits(hits):\n    return sorted(hits)\n"
    )
    (tmp_path / "src" / "app" / "deep.py").write_text(
        "x = 1\n" * 5000 + "class DeepSymbol:\n    pass\n"
    )
    (tmp_path / "src" / "app" / "bundle.js").write_text(
        "DeepSymbol;" * (MAX_SEARCH_BYTES // 10)
    )
    (tmp_path / "README.md").write_text("# Example\n\nA small example repository.\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    for noise in (".git", ".venv", "node_modules", "dist", "build", "__pycache__"):
        (tmp_path / noise).mkdir()
        (tmp_path / noise / "rank_hits.py").write_text("def rank_hits(): ...\n")
    return tmp_path


async def test_search_ranks_path_and_head(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))

    hits = await connector.search("rank_hits")
    assert [h.ref for h in hits] == ["repo:src/app/ranking.py"]
    assert hits[0].lead == "50: def rank_hits(hits):"
    assert hits[0].provenance.source_id == "repo"

    named = await connector.search("ranking")
    assert named[0].ref == "repo:src/app/ranking.py"


async def test_search_reads_past_the_head_and_skips_huge_files(repo: Path) -> None:
    hits = await CodeConnector("repo", str(repo)).search("deepsymbol")

    assert [h.ref for h in hits] == ["repo:src/app/deep.py"]
    assert hits[0].lead == "5001: class DeepSymbol:"


async def test_excluded_directories_are_never_listed(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))

    keys = {connector._key(p) for p in connector.files}
    assert keys == {
        "README.md",
        "src/app/bundle.js",
        "src/app/deep.py",
        "src/app/ranking.py",
    }
    assert connector.describe().count == 4


async def test_include_and_exclude_are_options(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo), include="*.md", exclude=".git src")
    assert [connector._key(p) for p in connector.files] == ["README.md"]


async def test_fetch_numbers_the_whole_file(repo: Path) -> None:
    doc = await CodeConnector("repo", str(repo)).fetch("README.md")
    assert doc.ref == "repo:README.md"
    assert doc.body.splitlines()[0] == "1  # Example"


async def test_fetch_reads_a_line_span(repo: Path) -> None:
    doc = await CodeConnector("repo", str(repo)).fetch("src/app/ranking.py:50-51")

    assert doc.ref == "repo:src/app/ranking.py:50-51"
    assert doc.body.splitlines() == [
        "50  def rank_hits(hits):",
        "51      return sorted(hits)",
    ]
    assert doc.metadata["lines"] == "50-51"


async def test_a_single_line_and_an_overlong_end(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))

    one = await connector.fetch("src/app/ranking.py:3")
    assert one.body == "3  line 3"

    clamped = await connector.fetch("src/app/ranking.py:50-900")
    assert clamped.ref == "repo:src/app/ranking.py:50-51"


async def test_a_span_starting_past_the_end_is_refused(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))
    with pytest.raises(ConnectError, match="past the end"):
        await connector.fetch("src/app/ranking.py:400-410")


@pytest.mark.parametrize("span", ["80-40", "0-3", "-5", "10-", "1-2-3"])
async def test_a_malformed_span_is_refused(repo: Path, span: str) -> None:
    connector = CodeConnector("repo", str(repo))
    with pytest.raises(ConnectError, match="not a line span"):
        await connector.fetch(f"src/app/ranking.py:{span}")


async def test_refuses_to_climb_out_of_its_root(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))
    with pytest.raises(ConnectError, match="outside"):
        await connector.fetch("../../../etc/passwd")
    with pytest.raises(ConnectError, match="outside"):
        await connector.fetch("../../../etc/passwd:1-2")


async def test_a_sibling_sharing_the_prefix_is_outside(tmp_path: Path) -> None:
    """`/repo2` starts with `/repo` as a string and is not inside it."""
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo2").mkdir()
    (tmp_path / "repo2" / "secret.py").write_text("token = 1\n")
    connector = CodeConnector("repo", str(tmp_path / "repo"))
    with pytest.raises(ConnectError, match="outside"):
        await connector.fetch("../repo2/secret.py")


async def test_unlisted_files_are_not_readable(repo: Path) -> None:
    """Excluded or unmatched files stay closed to fetch, not just hidden from search."""
    connector = CodeConnector("repo", str(repo))
    for key in (".env", ".git/rank_hits.py", "node_modules/rank_hits.py"):
        with pytest.raises(ConnectError, match="no file"):
            await connector.fetch(key)


async def test_tools_are_declared_read_only(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))
    specs = connector.tools()

    assert {s.name for s in specs} == {"search_code", "read_file", "list_files"}
    assert all(s.effect is Effect.EXTERNAL_READ for s in specs)


async def test_tools_read_and_list(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo))

    text = await connector.call(
        "read_file", {"path": "src/app/ranking.py", "start": 51, "end": 51}
    )
    assert text == "repo:src/app/ranking.py:51-51\n\n51      return sorted(hits)"

    found = await connector.call("search_code", {"query": "rank_hits"})
    assert found == (
        "repo:src/app/ranking.py — src/app/ranking.py\n  50: def rank_hits(hits):"
    )
    assert "no matches" in await connector.call("search_code", {"query": "absent"})

    py = "src/app/deep.py\nsrc/app/ranking.py"
    assert await connector.call("list_files", {"glob": "*.py"}) == py
    assert (
        await connector.call("list_files", {"subdir": "src/app", "glob": "*.py"}) == py
    )
    assert (await connector.call("list_files", {})).startswith("README.md\n")
    with pytest.raises(ConnectError, match="outside"):
        await connector.call("list_files", {"subdir": "../.."})
    with pytest.raises(ConnectError, match="no tool"):
        await connector.call("write_file", {})


async def test_declared_capabilities_are_verified_at_connect(repo: Path) -> None:
    connector = connectors.build("code", "repo", str(repo))
    await connectors.open_connector(connector)

    descriptor = connector.describe()
    assert descriptor.capabilities == [
        Capability.SEARCH,
        Capability.FETCH,
        Capability.TOOLS,
    ]
    assert descriptor.unit == "files"
    assert descriptor.detail == {"root": str(repo.resolve()), "graph": "no graph"}


def test_a_named_graph_is_refused(repo: Path) -> None:
    with pytest.raises(ConnectError, match="graph backends are not available yet"):
        CodeConnector("repo", str(repo), graph="codegraph")


def test_a_target_that_is_not_a_directory_is_refused(repo: Path) -> None:
    with pytest.raises(ConnectError, match="no directory"):
        CodeConnector("repo", str(repo / "README.md"))
    with pytest.raises(ConnectError, match="no directory"):
        CodeConnector("repo", str(repo / "missing"))


def test_options_survive_the_server_spec(repo: Path) -> None:
    connector = CodeConnector("repo", str(repo), include="*.md *.py")
    args = connector.server_spec()["args"]  # type: ignore[index]
    options = serve._options(
        [args[i + 1] for i, a in enumerate(args) if a == "--option"]
    )

    rebuilt = CodeConnector("repo", args[args.index("--target") + 1], **options)
    assert rebuilt.include == ["*.md", "*.py"]
    assert rebuilt.exclude == connector.exclude


async def test_an_empty_file_reads_as_empty(repo: Path) -> None:
    (repo / "src" / "app" / "__init__.py").write_text("")
    doc = await CodeConnector("repo", str(repo)).fetch("src/app/__init__.py")
    assert doc.body == ""


async def test_a_symlinked_file_is_not_listed_or_searched(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secrets.txt"
    outside.write_text("SECRET_TOKEN=1\n")
    (repo / "leak.txt").symlink_to(outside)
    connector = CodeConnector("repo", str(repo))

    assert "leak.txt" not in await connector.call("list_files", {})
    assert await connector.search("SECRET_TOKEN") == []


async def test_an_exclude_with_a_path_prunes_it(repo: Path) -> None:
    (repo / "src" / "gen").mkdir()
    (repo / "src" / "gen" / "g.py").write_text("generated = True\n")
    connector = CodeConnector("repo", str(repo), exclude="src/gen docs/* .git")

    assert "src/gen/g.py" not in await connector.call("list_files", {})
    with pytest.raises(ConnectError):
        await connector.fetch("src/gen/g.py")


async def test_read_file_output_carries_its_ref(repo: Path) -> None:
    output = await CodeConnector("repo", str(repo)).call(
        "read_file", {"path": "src/app/ranking.py", "start": 50, "end": 51}
    )
    assert output.startswith("repo:src/app/ranking.py:50-51\n")
