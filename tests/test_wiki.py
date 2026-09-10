"""The wiki connector, against a published store and nothing else.

The point of these tests is the claim the package now makes: a base install reads an
already-built wiki. So the fixture is a `manifest.json` and a `pages/` directory —
written here, byte for byte in the published shape — and `latent_wiki` is never
imported. If these pass with the extra uninstalled, the claim holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from latent_intel.connectors import wiki as wiki_module
from latent_intel.models import Capability, ConnectError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _page(key: str, frontmatter: dict, lead: str, body: str = "") -> dict:
    return {"key": key, "file": f"{key}.md", "lead": lead, "frontmatter": frontmatter}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A published store: one manifest, three pages, one of them retired."""
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)

    entries = [
        _page(
            "progressive-disclosure",
            {
                "type": "concept",
                "title": "Progressive Disclosure",
                "why": "The access pattern this corpus most agrees on.",
                "as_of": "2026-07-06",
                "tags": ["retrieval"],
                "links": [
                    {"to": "gist-ranking", "rel": "supports", "derivation": "extracted"}
                ],
                "sources": [{"resource": "corpus:raw/cochran2026.md"}],
            },
            "Give an agent identifiers first, and content once it has chosen.",
        ),
        _page(
            "gist-ranking",
            {"type": "concept", "title": "Gist Ranking", "why": "Rank over leads."},
            "Ranking over the lead paragraph beat ranking over the body.",
        ),
        _page(
            "disclosure-old",
            {
                "type": "concept",
                "title": "Progressive Disclosure (retired)",
                "status": "superseded",
                "superseded_by": "progressive-disclosure",
            },
            "An earlier account of progressive disclosure, kept for the record.",
        ),
    ]
    manifest = {
        "manifest_version": 1,
        "contract_version": 1,
        "store_id": "design",
        "pack": "design",
        "title": "Design Wiki",
        "description": "AI engineering and knowledge systems.",
        "generated": "2026-08-21",
        "page_count": len(entries),
        "pages": entries,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for entry in entries:
        (root / "pages" / entry["file"]).write_text(
            f"---\ntitle: {entry['frontmatter']['title']}\n---\n\n{entry['lead']}\n",
            encoding="utf-8",
        )
    return root


def connect(root: Path, **options) -> wiki_module.WikiConnector:
    return wiki_module.WikiConnector("design", str(root), **options)


# -- the claim: no latent_wiki, no extra --------------------------------------


def _without_latent_wiki(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Make `latent_wiki` unimportable, so a base install is what is under test.

    The extra is installed on a development machine, which is exactly why this has to be
    forced: a test that passes only where the producer is installed proves nothing about
    the install a client gets.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("latent_wiki"):
            raise error("the wiki connector reached for latent_wiki")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)


def test_reads_a_published_store_without_latent_wiki(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base-install guarantee, enforced rather than asserted in prose."""
    _without_latent_wiki(monkeypatch, AssertionError)
    descriptor = connect(store).describe()
    assert descriptor.count == 3
    assert descriptor.title == "Design Wiki"
    assert descriptor.freshness == "2026-08-21"
    assert descriptor.detail["from_manifest"] is True
    assert Capability.SEARCH in descriptor.capabilities


def test_an_unreadable_store_says_which_case_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three roots fail differently, so they must not share one message.

    A deployment hosting its store on S3 keeps no local copy by design. Telling that
    machine its absent directory "has no manifest.json" sent it chasing a publish step
    for something that was never there.
    """
    _without_latent_wiki(monkeypatch, ImportError)

    absent = tmp_path / "gone"
    empty = tmp_path / "empty"
    empty.mkdir()
    unpublished = tmp_path / "unpublished"
    (unpublished / "pages").mkdir(parents=True)
    (unpublished / "pages" / "a.md").write_text("---\ntitle: A\n---\nbody\n")

    for root, expected in (
        (absent, "does not exist"),
        (empty, "is empty"),
        (unpublished, "unpublished store"),
    ):
        with pytest.raises(ConnectError) as caught:
            connect(root)
        assert expected in str(caught.value), f"{root.name}: {caught.value}"


def test_no_failure_names_a_package_the_reader_cannot_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client reads a published store; the tool that writes one is not its business.

    Naming that tool in an error is advice a client machine cannot act on, and it names
    a package that is not published.
    """
    _without_latent_wiki(monkeypatch, ImportError)
    root = tmp_path / "unpublished"
    root.mkdir()
    (root / "pages").mkdir()
    with pytest.raises(ConnectError) as caught:
        connect(root)
    message = str(caught.value)
    for leak in ("latent-wiki", "latent_wiki", "lw manifest", "just publish", "[wiki]"):
        assert leak not in message, f"error names {leak}: {message}"


def test_a_future_manifest_is_refused_not_misread(store: Path) -> None:
    data = json.loads((store / "manifest.json").read_text())
    data["manifest_version"] = 2
    (store / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConnectError) as caught:
        connect(store)
    assert "manifest_version 2" in str(caught.value)


def test_malformed_manifest_raises_rather_than_degrading(store: Path) -> None:
    (store / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConnectError):
        connect(store)


# -- the two measured ranking decisions ---------------------------------------


@pytest.mark.anyio
async def test_filters_before_ranking(store: Path) -> None:
    """A superseded page is excluded by predicate, however well it matches."""
    hits = await connect(store).search("progressive disclosure")
    assert [h.ref for h in hits] == ["design:progressive-disclosure"]

    with_retired = await connect(store).search(
        "progressive disclosure", include_retired=True
    )
    assert "design:disclosure-old" in [h.ref for h in with_retired]


@pytest.mark.anyio
async def test_key_and_title_outrank_prose(store: Path) -> None:
    hits = await connect(store).search("gist")
    assert hits[0].ref == "design:gist-ranking"
    # key (5) + title (3) + one occurrence each in key, title and lead.
    assert hits[0].score >= 8


@pytest.mark.anyio
async def test_type_and_tag_filters(store: Path) -> None:
    connector = connect(store)
    assert await connector.search("disclosure", type="source") == []
    tagged = await connector.search("disclosure", tag="retrieval")
    assert [h.ref for h in tagged] == ["design:progressive-disclosure"]


@pytest.mark.anyio
async def test_search_carries_provenance(store: Path) -> None:
    hits = await connect(store).search("progressive disclosure")
    provenance = hits[0].provenance
    assert provenance.origin == "corpus:raw/cochran2026.md"
    assert provenance.as_of == "2026-07-06"
    assert provenance.method == "search"


# -- fetch is the deferred call -----------------------------------------------


@pytest.mark.anyio
async def test_fetch_reads_the_body_and_strips_frontmatter(store: Path) -> None:
    doc = await connect(store).fetch("progressive-disclosure")
    assert doc.body.startswith("Give an agent identifiers")
    assert "---" not in doc.body
    assert doc.kind == "concept"
    assert doc.metadata["links"][0]["to"] == "gist-ranking"


@pytest.mark.anyio
async def test_fetch_names_the_source_it_could_not_find(store: Path) -> None:
    with pytest.raises(ConnectError) as caught:
        await connect(store).fetch("nope")
    assert "no page 'nope' in design" in str(caught.value)


# -- graph and escalation -----------------------------------------------------


def test_neighbours_are_bidirectional(store: Path) -> None:
    connector = connect(store)
    assert connector.neighbors("progressive-disclosure")["out"] == [
        ("gist-ranking", "extracted")
    ]
    assert connector.neighbors("gist-ranking")["in"] == [
        ("progressive-disclosure", "extracted")
    ]


def test_trace_reports_lifecycle(store: Path) -> None:
    trace = connect(store).trace("disclosure-old")
    assert trace["status"] == "superseded"
    assert trace["superseded_by"] == "progressive-disclosure"


def test_raw_reads_the_paired_context(store: Path, tmp_path: Path) -> None:
    context = tmp_path / "context"
    (context / "raw").mkdir(parents=True)
    (context / "raw" / "cochran2026.md").write_text("the underlying paper", "utf-8")

    connector = connect(store, context=str(context))
    assert "the underlying paper" in connector.raw("progressive-disclosure")


def test_raw_without_context_says_what_to_pair(store: Path) -> None:
    answer = connect(store).raw("progressive-disclosure")
    assert "context:" in answer


def test_raw_pages_a_long_source(store: Path, tmp_path: Path) -> None:
    context = tmp_path / "context"
    (context / "raw").mkdir(parents=True)
    (context / "raw" / "cochran2026.md").write_text("x" * 100, "utf-8")

    answer = connect(store, context=str(context)).raw(
        "progressive-disclosure", offset=0, max_chars=40
    )
    assert "offset=40" in answer


# -- tools are read-only, and declared ----------------------------------------


def test_every_tool_declares_a_read_effect(store: Path) -> None:
    tools = connect(store).tools()
    assert {t.name for t in tools} == {
        "wiki_search",
        "wiki_get",
        "wiki_neighbors",
        "wiki_trace",
        "wiki_raw",
    }
    assert all(t.effect.value == "external_read" for t in tools)
    assert all(t.effect_declared for t in tools)


@pytest.mark.anyio
async def test_unknown_tool_is_refused(store: Path) -> None:
    with pytest.raises(ConnectError):
        await connect(store).call("wiki_write", {})


class _RemoteRoot:
    """A stand-in for an `s3://` root whose reads fail the way s3fs fails them."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __truediv__(self, name: str) -> _RemoteRoot:
        return self

    def read_text(self, encoding: str = "utf-8") -> str:
        raise self.error

    def exists(self) -> bool:
        return False

    def is_dir(self) -> bool:
        return False

    def __str__(self) -> str:
        return "s3://bucket/wikis/w"


def test_a_refused_remote_read_says_refused_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over fsspec, `is_file()` answers False to a 403 as readily as to a 404, so the
    connector must read and report the refusal — a client with the wrong key was being
    told its store did not exist."""
    _without_latent_wiki(monkeypatch, ImportError)
    refused = _RemoteRoot(PermissionError("Forbidden"))
    monkeypatch.setattr(wiki_module, "_as_path", lambda target: refused)
    with pytest.raises(ConnectError) as caught:
        wiki_module.WikiConnector("w", "s3://bucket/wikis/w")
    message = str(caught.value)
    assert "Forbidden" in message
    assert "refused" in message
    assert "does not exist" not in message


def test_a_missing_remote_manifest_still_reports_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_latent_wiki(monkeypatch, ImportError)
    monkeypatch.setattr(
        wiki_module, "_as_path", lambda target: _RemoteRoot(FileNotFoundError("404"))
    )
    with pytest.raises(ConnectError) as caught:
        wiki_module.WikiConnector("w", "s3://bucket/wikis/w")
    assert "does not exist" in str(caught.value)
