"""The wiki connector against the producer's published-format fixtures.

`tests/fixtures/formats/{wiki-store,context-store}` are copies of latent-wiki's samples:
a stamped manifest, two pages (one a draft), and the context store they cite. These
tests are the reader's half of that contract, and of the disclosure ladder built on it:
overview → index / search → get → neighbors / trace → evidence → raw.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import fsspec
import pytest

from latent_intel.connectors import wiki as wiki_module
from latent_intel.models import ConnectError

FORMATS = Path(__file__).resolve().parent / "fixtures" / "formats"
WIKI = FORMATS / "wiki-store"
CONTEXT = FORMATS / "context-store"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def connect(root: Path = WIKI, **options: str) -> wiki_module.WikiConnector:
    options.setdefault("context", str(CONTEXT))
    return wiki_module.WikiConnector("heron", str(root), **options)


def copied(tmp_path: Path) -> tuple[Path, Path]:
    wiki, context = tmp_path / "wiki", tmp_path / "context"
    shutil.copytree(WIKI, wiki)
    shutil.copytree(CONTEXT, context)
    return wiki, context


# -- the contract -------------------------------------------------------------


def test_reads_the_published_format_fixture() -> None:
    connector = connect()
    assert set(connector.pages) == {"tide-pool", "count-limpets"}


def test_an_unstamped_manifest_is_refused(tmp_path: Path) -> None:
    wiki, _ = copied(tmp_path)
    data = json.loads((wiki / "manifest.json").read_text())
    del data["format"], data["format_version"]
    (wiki / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(ConnectError, match="republish"):
        connect(wiki)


def test_an_unknown_format_version_is_refused(tmp_path: Path) -> None:
    wiki, _ = copied(tmp_path)
    data = json.loads((wiki / "manifest.json").read_text())
    data["format_version"] = 2
    (wiki / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(ConnectError, match="v2"):
        connect(wiki)


def test_source_docs_count_as_resources() -> None:
    page = connect().pages["tide-pool"]
    assert page.resources() == ["corpus:kelpmoor-ch01-pools"]
    assert connect().trace("tide-pool")["source_docs"] == ["corpus:kelpmoor-ch01-pools"]


# -- search -------------------------------------------------------------------


@pytest.mark.anyio
async def test_fts_ranks_title_over_lead() -> None:
    hits = await connect().search("limpets")
    assert hits[0].ref == "heron:count-limpets"
    assert "title" in hits[0].metadata["matched_on"]


@pytest.mark.anyio
async def test_stemming_meets_plural_and_singular() -> None:
    hits = await connect().search("pools")
    assert hits and hits[0].ref == "heron:tide-pool"


@pytest.mark.anyio
async def test_alias_exact_hit_is_pinned() -> None:
    hits = await connect().search("rock pool")
    assert hits[0].ref == "heron:tide-pool"
    assert hits[0].metadata["exact"] is True


@pytest.mark.anyio
async def test_filter_on_arbitrary_frontmatter_field() -> None:
    connector = connect()
    stepped = await connector.search("pool limpets", where={"step": 1})
    assert [h.ref for h in stepped] == ["heron:count-limpets"]
    assert await connector.search("pool", where={"step": [2, 3]}) == []


@pytest.mark.anyio
async def test_drafts_are_flagged_and_can_be_excluded() -> None:
    connector = connect()
    hits = await connector.search("limpets")
    assert hits[0].metadata["draft"] is True
    assert await connector.search("limpets", include_drafts=False) == []


@pytest.mark.anyio
@pytest.mark.parametrize("query", ['tide-pool: "', "NOT AND OR", "pool*", "(limpets"])
async def test_fts_query_with_operators_does_not_raise(query: str) -> None:
    await connect().search(query)


@pytest.mark.anyio
async def test_a_question_with_extra_words_still_finds_the_page() -> None:
    """All terms first; any term when that finds nothing."""
    hits = await connect().search("how do I count limpets at low water")
    assert hits[0].ref == "heron:count-limpets"


# -- the ladder ---------------------------------------------------------------


def test_overview_names_types_and_lists_small_ones() -> None:
    text = connect().overview()
    assert "2 pages (1 draft" in text
    assert "concept: 1" in text and "procedure: 1" in text
    assert "count-limpets — Count limpets · draft" in text
    assert "Next:" in text


def test_index_is_paged() -> None:
    text = connect().index(limit=1)
    assert "next offset=1" in text
    assert "1 of 2" in text


def test_get_section_and_continuation() -> None:
    connector = connect()
    full = connector.get("tide-pool")
    assert "sections: What the sources say" in full
    assert "evidence: 1 entries" in full
    section = connector.get("tide-pool", section="what the sources")
    assert section.split("\n\n", 1)[1].startswith("## What the sources say")
    missing = connector.get("tide-pool", section="nowhere")
    assert "sections:" in missing


def test_get_states_a_cut(tmp_path: Path) -> None:
    wiki, context = copied(tmp_path)
    page = wiki / "pages" / "tide-pool.md"
    page.write_text(page.read_text() + "x" * (wiki_module.GET_MAX_CHARS + 50))
    text = connect(wiki, context=str(context)).get("tide-pool")
    assert f"offset={wiki_module.GET_MAX_CHARS}" in text


def test_evidence_resolves_corpus_key_to_summary() -> None:
    text = connect().evidence("tide-pool")
    assert "kelpmoor-ch01-pools — Kelpmoor Survey Manual — Ch. 1: Pools" in text
    assert "why: Defines a tide pool" in text
    full = connect().evidence("tide-pool", entry="corpus:kelpmoor-ch01-pools")
    assert "prescribes counting limpets" in full
    assert "wiki_raw key=kelpmoor-ch01-pools" in full


def test_evidence_refuses_an_unstamped_context(tmp_path: Path) -> None:
    wiki, context = copied(tmp_path)
    descriptor = context / "context-store.yaml"
    descriptor.write_text(
        descriptor.read_text().replace("format: latent-wiki.context\n", "")
    )
    text = connect(wiki, context=str(context)).evidence("tide-pool")
    assert "latent-wiki.context" in text


def test_evidence_without_context_says_what_to_pair() -> None:
    connector = wiki_module.WikiConnector("heron", str(WIKI))
    assert "context:" in connector.evidence("tide-pool")


def test_raw_follows_summary_raw_path() -> None:
    connector = connect()
    by_page = connector.raw("tide-pool")
    assert "Count the limpets." in by_page
    by_entry = connector.raw("kelpmoor-ch01-pools")
    assert "Count the limpets." in by_entry


def test_raw_refuses_a_path_that_leaves_the_store(tmp_path: Path) -> None:
    wiki, context = copied(tmp_path)
    summary = context / "summaries" / "kelpmoor-ch01-pools.md"
    summary.write_text(
        summary.read_text().replace("raw/kelpmoor/01-pools.md", "../outside.md")
    )
    (tmp_path / "outside.md").write_text("should never be read")
    assert "should never" not in connect(wiki, context=str(context)).raw("tide-pool")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("wiki_overview", {}),
        ("wiki_index", {}),
        ("wiki_search", {"query": "pool"}),
        ("wiki_search", {"query": "nothing-matches-this"}),
        ("wiki_get", {"key": "tide-pool"}),
        ("wiki_neighbors", {"key": "tide-pool"}),
        ("wiki_trace", {"key": "tide-pool"}),
        ("wiki_evidence", {"key": "tide-pool"}),
    ],
)
async def test_every_response_states_where_and_what_was_cut(
    tool: str, arguments: dict
) -> None:
    text = await connect().call(tool, arguments)
    assert "— " in text and "heron" in text
    assert "cut:" in text


def test_remote_context_relative_raw_path(tmp_path: Path) -> None:
    """Over a URI root, `raw_path` joins the context root, never via `Path`."""
    fs = fsspec.filesystem("memory")
    for local, prefix in ((WIKI, "/heron/wiki"), (CONTEXT, "/heron/context")):
        for path in local.rglob("*"):
            if path.is_file():
                fs.pipe(f"{prefix}/{path.relative_to(local)}", path.read_bytes())
    connector = wiki_module.WikiConnector(
        "heron", "memory:///heron/wiki", context="memory:///heron/context"
    )
    assert "Count the limpets." in connector.raw("tide-pool")
    assert "kelpmoor-ch01-pools" in connector.evidence("tide-pool")
    fs.rm("/heron", recursive=True)


# -- review regressions -------------------------------------------------------


@pytest.mark.parametrize("entry", ["../../outside", "/etc/hosts", "corpus:../x", "a/b"])
def test_an_entry_key_cannot_name_a_path(tmp_path: Path, entry: str) -> None:
    wiki, context = copied(tmp_path)
    (tmp_path / "outside.md").write_text("should never be read")
    connector = connect(wiki, context=str(context))
    assert "should never" not in connector.evidence("tide-pool", entry=entry)
    assert "should never" not in connector.raw(entry)


def test_a_declared_source_cannot_leave_the_store(tmp_path: Path) -> None:
    wiki, context = copied(tmp_path)
    (tmp_path / "outside.md").write_text("should never be read")
    data = json.loads((wiki / "manifest.json").read_text())
    data["pages"][1]["frontmatter"]["sources"] = [{"resource": "corpus:../outside.md"}]
    (wiki / "manifest.json").write_text(json.dumps(data))
    key = data["pages"][1]["key"]
    assert "should never" not in connect(wiki, context=str(context)).raw(key)


def test_any_term_still_runs_when_every_all_terms_match_is_filtered() -> None:
    from latent_intel.connectors import _fts

    index = _fts.Index(
        [
            _fts.Row("old", {"title": "tide pool limpets"}),
            _fts.Row("new", {"title": "tide pools"}),
        ]
    )
    assert [r.key for r in index.rank("tide pool limpets", {"new"})] == ["new"]


@pytest.mark.anyio
async def test_raw_sizes_are_clamped() -> None:
    text = await connect().call(
        "wiki_raw", {"key": "tide-pool", "max_chars": 10**9, "offset": -5}
    )
    assert "[0:" in text


@pytest.mark.anyio
async def test_index_lists_one_parents_children(tmp_path: Path) -> None:
    wiki, context = copied(tmp_path)
    data = json.loads((wiki / "manifest.json").read_text())
    for entry in data["pages"]:
        if entry["key"] == "count-limpets":
            entry["frontmatter"]["part_of"] = "tide-pool"
    (wiki / "manifest.json").write_text(json.dumps(data))
    text = await connect(wiki, context=str(context)).call(
        "wiki_index", {"part_of": "tide-pool"}
    )
    assert "count-limpets" in text and "[concept] tide-pool" not in text


def test_evidence_list_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki, context = copied(tmp_path)
    data = json.loads((wiki / "manifest.json").read_text())
    for entry in data["pages"]:
        if entry["key"] == "tide-pool":
            entry["frontmatter"]["source_docs"] = [
                "corpus:kelpmoor-ch01-pools",
                "corpus:kelpmoor-ch02-missing",
            ]
    (wiki / "manifest.json").write_text(json.dumps(data))
    monkeypatch.setattr(wiki_module, "EVIDENCE_MAX", 1)
    text = connect(wiki, context=str(context)).evidence("tide-pool")
    assert "1 of 2 entries" in text


def test_a_malformed_summary_is_reported_not_raised(tmp_path: Path) -> None:
    wiki, context = copied(tmp_path)
    summary = context / "summaries" / "kelpmoor-ch01-pools.md"
    summary.write_text("---\nkey: [unclosed\n---\nBody.\n")
    text = connect(wiki, context=str(context)).evidence("tide-pool")
    assert "not YAML" in text


@pytest.mark.anyio
async def test_search_reports_a_cut_only_when_there_is_more() -> None:
    exact = await connect().call("wiki_search", {"query": "pool", "limit": 2})
    assert "cut: nothing" in exact
    capped = await connect().call("wiki_search", {"query": "pool", "limit": 1})
    assert "first 1" in capped


def test_index_body_reaches_the_served_process() -> None:
    spec = connect(index_body="true").server_spec()
    assert spec is not None
    assert "index_body=true" in " ".join(spec["args"])


def test_a_heading_inside_a_code_fence_is_not_structure() -> None:
    body = (
        "## Usage\n\nRun it:\n\n```bash\n## install deps\npip install x\n```\n\n"
        "Done.\n\n# Appendix\n\nOther."
    )
    assert wiki_module._section(body, "usage").endswith("Done.")
    assert [t for _, t in wiki_module._headings(body)] == ["Usage"]
