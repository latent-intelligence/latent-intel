"""The session and the connector seam.

Nothing here touches a real store, a network or a model. The fixtures are a temporary
directory and a deliberately broken connector — which is the point: if these needed a
Drive path or an S3 bucket, they would not run in CI and the seam would be unverified.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from latent_intel import events as ev
from latent_intel import registry
from latent_intel.commands import Connect, Fetch, Find, ListSources
from latent_intel.connectors import base as connectors
from latent_intel.connectors.files import FilesConnector
from latent_intel.models import (
    Capability,
    CapabilityError,
    ConnectError,
    Descriptor,
    Effect,
    Ref,
    RuntimeUnavailable,
    SessionError,
    SourceRequest,
    ToolSpec,
)
from latent_intel.session import Session

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "notes").mkdir()
    (tmp_path / "retrieval.md").write_text(
        "---\ntitle: ignored\n---\n\n"
        "# Retrieval\n\n"
        "Retrieval is how a system finds material before generating.\n"
    )
    (tmp_path / "notes" / "compaction.md").write_text(
        "# Compaction\n\nRewriting an accumulated context destroys it.\n"
    )
    return tmp_path


# -- the files connector ----------------------------------------------------


async def test_files_connector_searches_and_fetches(corpus: Path) -> None:
    connector = FilesConnector("notes", str(corpus))
    descriptor = connector.describe()
    assert descriptor.capabilities == [
        Capability.SEARCH,
        Capability.FETCH,
        Capability.TOOLS,
    ]
    assert descriptor.count == 2

    hits = await connector.search("retrieval")
    assert [h.ref for h in hits] == ["notes:retrieval.md"]
    assert hits[0].title == "Retrieval"
    assert hits[0].lead.startswith("Retrieval is how a system finds")
    assert hits[0].provenance.source_id == "notes"

    doc = await connector.fetch("notes/compaction.md")
    assert doc.title == "Compaction"
    assert "destroys it" in doc.body


async def test_files_connector_refuses_to_climb_out_of_its_root(corpus: Path) -> None:
    """A key arrives from a user, a config file or an agent. `../../.ssh/id_rsa` is a
    perfectly valid relative path and must not be a valid key."""
    connector = FilesConnector("notes", str(corpus))
    with pytest.raises(ConnectError, match="outside"):
        await connector.fetch("../../../etc/passwd")


def test_files_connector_rejects_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ConnectError, match="no directory"):
        FilesConnector("nope", str(tmp_path / "does-not-exist"))


# -- capability honesty -----------------------------------------------------


class LyingConnector:
    """Claims search, implements nothing. The failure this catches is a search that
    returns nothing and looks like an empty corpus."""

    kind = "liar"

    def __init__(self, source_id: str, target: str, **options: Any) -> None:
        self.id = source_id

    def describe(self) -> Descriptor:
        return Descriptor(id=self.id, kind=self.kind, capabilities=[Capability.SEARCH])

    async def aclose(self) -> None:
        return None


class ToolsOnlyConnector:
    """What an MCP server usually is: tools, no search, no fetch."""

    kind = "mcp"

    def __init__(self, source_id: str, target: str, **options: Any) -> None:
        self.id = source_id

    def describe(self) -> Descriptor:
        return Descriptor(id=self.id, kind=self.kind, capabilities=[Capability.TOOLS])

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="do_thing", source_id=self.id, effect=Effect.EXTERNAL_READ)
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return "done"

    async def aclose(self) -> None:
        return None


class _Unservable:
    """In-process only: no `server_spec`, so no subprocess agent can reach it."""

    kind = "plain"

    def __init__(self, source_id: str, target: str, **options: Any) -> None:
        self.id = source_id

    def describe(self) -> Descriptor:
        return Descriptor(id=self.id, kind=self.kind, capabilities=[])

    async def aclose(self) -> None:
        return None


def test_a_connector_cannot_claim_what_it_does_not_implement() -> None:
    with pytest.raises(ConnectError, match="does not implement"):
        liar = LyingConnector("liar", "")
        connectors.verify_capabilities(liar)  # type: ignore[arg-type]


def test_our_own_adapters_are_registered_through_entry_points() -> None:
    """The plug-in path is the one we take ourselves, so it cannot rot unnoticed.

    `available_kinds` reads the same entry-point group a third party publishes into; if
    this passes, a third-party connector installed the same way is discoverable too.
    """
    kinds = connectors.available_kinds()
    assert "files" in kinds
    assert kinds["files"] is FilesConnector


def test_an_unknown_kind_names_what_is_installed() -> None:
    with pytest.raises(ConnectError, match="installed:"):
        connectors.build("telepathy", "x", "y")


# -- refs -------------------------------------------------------------------


def test_ref_parsing_follows_the_colon_rule() -> None:
    assert Ref.parse("design:context-collapse").key == "context-collapse"
    assert Ref.parse("bare", default_source="design").source_id == "design"
    # A URL is not a ref — reading one as though it were would address another store.
    assert Ref.parse("https://x/y", default_source="d").key == "https://x/y"
    with pytest.raises(ValueError, match="no source prefix"):
        Ref.parse("bare")


# -- the session ------------------------------------------------------------


async def test_search_groups_by_source_and_never_merges(corpus: Path) -> None:
    """The one structural guarantee: two sources, two result sets, no shared ordering.

    If this ever returns a flat list, a lexical count and a vector cosine end up sorted
    against each other on a number that does not mean the same thing.
    """
    session = Session()
    await session.connect(str(corpus), kind="files", source_id="a")
    await session.connect(str(corpus), kind="files", source_id="b")

    grouped = await session.find("retrieval")
    assert list(grouped) == ["a", "b"]
    assert all(len(hits) == 1 for hits in grouped.values())

    events = [e async for e in session.run(Find(query="retrieval"))]
    results = [e for e in events if isinstance(e, ev.RetrievalResult)]
    assert [r.source_id for r in results] == ["a", "b"]
    await session.aclose()


async def test_fetch_resolves_a_bare_key_against_the_current_source(
    corpus: Path,
) -> None:
    session = Session()
    await session.connect(str(corpus), kind="files", source_id="a")
    assert session.current == "a"

    doc = await session.fetch("retrieval.md")
    assert doc.ref == "a:retrieval.md"
    await session.aclose()


async def test_connecting_twice_under_one_id_is_refused(corpus: Path) -> None:
    session = Session()
    await session.connect(str(corpus), kind="files", source_id="a")
    with pytest.raises(ConnectError, match="already connected"):
        await session.connect(str(corpus), kind="files", source_id="a")
    await session.aclose()


async def test_fetching_from_an_unconnected_source_says_so(corpus: Path) -> None:
    session = Session()
    await session.connect(str(corpus), kind="files", source_id="a")
    with pytest.raises(SessionError, match="not connected"):
        await session.fetch("elsewhere:key")
    await session.aclose()


async def test_a_tools_only_source_is_not_searched(corpus: Path) -> None:
    """The reason capabilities compose. An MCP server offering only tools must not be
    asked to search — and must not be silently skipped either, when named explicitly."""
    session = Session()
    await session.connect(str(corpus), kind="files", source_id="a")
    session._connectors["gh"] = ToolsOnlyConnector("gh", "")  # type: ignore[assignment]

    # Named explicitly: a clear refusal.
    with pytest.raises(CapabilityError, match="does not support search"):
        await session.find("x", source="gh")

    # Searched implicitly: skipped, because it cannot contribute.
    grouped = await session.find("retrieval")
    assert list(grouped) == ["a"]

    # But its tools still reach the router, alongside the directory's own.
    assert [t.qualified for t in session.tools()] == [
        "a.files_search",
        "a.files_get",
        "gh.do_thing",
    ]
    await session.aclose()


async def test_disconnect_moves_the_current_source(corpus: Path) -> None:
    session = Session()
    await session.connect(str(corpus), kind="files", source_id="a")
    await session.connect(str(corpus), kind="files", source_id="b")
    assert session.current == "a"
    await session.disconnect("a")
    assert session.current == "b"
    await session.aclose()


# -- run(): failures are events ---------------------------------------------


async def test_run_turns_failures_into_events_not_exceptions() -> None:
    """A frontend iterating this over a transport has nowhere to catch an exception."""
    session = Session()
    events = [e async for e in session.run(Fetch(ref="nowhere:key"))]
    assert len(events) == 1
    assert isinstance(events[0], ev.AgentFailed)
    assert "not connected" in events[0].message


async def test_ask_reports_that_no_runtime_is_configured() -> None:
    """Explicit, not a silent no-op that would look like a model with nothing to say."""
    from latent_intel.commands import Ask

    session = Session()
    events = [e async for e in session.run(Ask(prompt="why"))]
    failures = [e for e in events if isinstance(e, ev.AgentFailed)]
    assert failures and "no agent runtime configured" in failures[0].message
    assert "intel doctor" in failures[0].remedy


async def test_run_emits_a_connect_event_with_a_descriptor(corpus: Path) -> None:
    session = Session()
    events = [
        e
        async for e in session.run(
            Connect(spec=str(corpus), kind="files", source_id="a")
        )
    ]
    connected = [e for e in events if isinstance(e, ev.SourceConnected)]
    assert connected and connected[0].descriptor.id == "a"

    listed = [e async for e in session.run(ListSources())]
    assert len(listed) == 1
    await session.aclose()


async def test_every_run_event_survives_json(corpus: Path) -> None:
    """The same guarantee the recorded fixtures assert, but on live output — a field
    that only works in-process would otherwise be caught by neither."""
    session = Session()
    async for event in session.run(
        Connect(spec=str(corpus), kind="files", source_id="a")
    ):
        assert ev.parse_event(ev.dump_event(event)) == event
    async for event in session.run(Find(query="retrieval")):
        assert ev.parse_event(ev.dump_event(event)) == event
    await session.aclose()


# -- the registry -----------------------------------------------------------


def test_unknown_registry_id_lists_what_is_known(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "manifest.json").write_text("{}")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "note.md").write_text("x")
    path = tmp_path / "stores.yaml"
    path.write_text(
        f"stores:\n"
        f"  - {{id: alpha, kind: wiki, paths: {{root: {tmp_path}/a}}}}\n"
        f"  - {{id: beta, kind: context-store, paths: {{raw: {tmp_path}/b}}}}\n"
    )
    assert registry.resolve("beta", str(path)) == ("files", f"{tmp_path}/b")
    with pytest.raises(ConnectError, match="alpha, beta"):
        registry.resolve("gamma", str(path))


def test_a_registry_entry_falls_back_to_its_mirror(tmp_path: Path) -> None:
    """Registry paths are machine-specific. A Drive path that exists on a laptop does
    not exist on a build host, so preferring local unconditionally makes every entry
    unusable anywhere else."""
    path = tmp_path / "stores.yaml"
    path.write_text(
        f"stores:\n"
        f"  - id: w\n"
        f"    kind: wiki\n"
        f"    paths: {{root: {tmp_path}/absent, mirror: 's3://bucket/w'}}\n"
    )
    assert registry.resolve("w", str(path)) == ("wiki", "s3://bucket/w")

    # An empty directory is a placeholder, not a store. A synced folder leaves one on a
    # machine that never pulled the data, and a deployment that keeps no local copy by
    # design has exactly that — it must still reach the mirror.
    local = tmp_path / "absent"
    local.mkdir()
    registry.clear_cache()
    assert registry.resolve("w", str(path)) == ("wiki", "s3://bucket/w")

    (local / "manifest.json").write_text("{}")  # now the local copy is really there
    registry.clear_cache()
    assert registry.resolve("w", str(path)) == ("wiki", str(local))
    # …and --remote still forces the mirror, which is how you check it is current.
    assert registry.resolve("w", str(path), remote=True) == ("wiki", "s3://bucket/w")


def test_remote_without_a_mirror_says_what_to_do(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    path = tmp_path / "stores.yaml"
    path.write_text(
        f"stores:\n  - {{id: w, kind: wiki, paths: {{root: {tmp_path}/a}}}}\n"
    )
    with pytest.raises(ConnectError, match="declares no mirror"):
        registry.resolve("w", str(path), remote=True)


def test_a_missing_registry_is_empty_not_an_error(tmp_path: Path) -> None:
    assert registry.load(str(tmp_path / "absent.yaml")) == {}


def test_an_mcp_access_entry_maps_to_the_mcp_kind(tmp_path: Path) -> None:
    path = tmp_path / "stores.yaml"
    path.write_text("stores:\n  - {id: gh, kind: saas, access: 'mcp:github-server'}\n")
    assert registry.resolve("gh", str(path)) == ("mcp", "github-server")


class FakeRuntime:
    """A runtime with no subprocess — proves `ask` reaches the seam."""

    id = "fake"

    def available(self) -> bool:
        return True

    async def stream(self, messages, tools, *, emitter, **options):  # type: ignore[no-untyped-def]
        self.saw = {"messages": list(messages), "options": options}
        yield emitter.emit(ev.AssistantToken, text="hi")
        yield emitter.emit(ev.AgentCompleted, text="hi", streamed=True)


@pytest.mark.anyio
async def test_ask_reaches_the_runtime_and_records_the_turn() -> None:
    runtime = FakeRuntime()
    session = Session(runtime=runtime)
    events = [e async for e in session.ask("what is compaction?")]

    assert [type(e).__name__ for e in events] == ["AssistantToken", "AgentCompleted"]
    assert runtime.saw["messages"][0].text == "what is compaction?"
    # The session builds the Emitter, so a runtime author never touches a sequence
    # number — the same bargain connectors get by returning values.
    assert events[0].sequence < events[1].sequence


def test_an_unknown_runtime_names_what_is_installed() -> None:
    session = Session()
    with pytest.raises(RuntimeUnavailable, match="installed:"):
        session.set_runtime("telepathy")


def test_runtime_kinds_reports_whether_each_can_run() -> None:
    kinds = Session.runtime_kinds()
    assert "claude-cli" in kinds and isinstance(kinds["claude-cli"], bool)


@pytest.mark.anyio
async def test_a_source_this_client_can_read_the_agent_can_reach() -> None:
    """The rule that replaced `shutil.which("lw")`.

    Agent access used to depend on another package's console script being on `PATH`, so
    a source the client read perfectly was invisible to the agent for a reason the
    operator could not see. Now the launcher is the interpreter already running.
    """
    session = Session()
    await session.connect(str(Path(__file__).parent), kind="files", source_id="files")

    spec = session.mcp_servers()["files"]
    assert spec["command"] == sys.executable
    assert spec["args"][:2] == ["-m", "latent_intel.serve"]
    await session.aclose()


async def test_a_source_with_no_tools_is_absent_rather_than_broken() -> None:
    """Better absent than silently missing: a source that cannot be served is not
    offered to the agent at all, and `doctor` says so."""
    session = Session()
    session._connectors["plain"] = _Unservable("plain", "")  # type: ignore[assignment]
    assert session.mcp_servers() == {}


def test_no_test_can_see_the_real_registry() -> None:
    """The guard for the guard in `conftest.isolated_home`.

    Before that fixture, `KNOWLEDGE_REGISTRY` was isolated nowhere and this returned
    seven real store ids on a developer machine. If someone removes that line, this
    fails rather than the suite quietly reading whatever host it runs on.
    """
    assert Session().available() == []


def test_a_registry_is_parsed_once_however_many_sources_name_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve` is called once per source, so six named sources parsed the same file
    seven times. Assert the count, because the count is the whole point."""
    stores = tmp_path / "stores.yaml"
    rows = "\n".join(
        f"  - id: s{n}\n    kind: context-store\n    paths: {{root: {tmp_path}}}"
        for n in range(6)
    )
    stores.write_text(f"stores:\n{rows}\n", encoding="utf-8")
    registry.clear_cache()

    parses = 0
    real = registry.yaml.safe_load

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal parses
        parses += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(registry.yaml, "safe_load", counting)
    for n in range(6):
        registry.resolve(f"s{n}", str(stores))

    assert parses == 1


def test_editing_a_registry_mid_session_is_picked_up(
    tmp_path: Path,
) -> None:
    """The mtime is in the cache key, so a stale entry cannot outlive an edit."""
    stores = tmp_path / "stores.yaml"
    stores.write_text(
        f"stores:\n  - id: one\n    kind: context-store\n"
        f"    paths: {{root: {tmp_path}}}\n",
        encoding="utf-8",
    )
    registry.clear_cache()
    assert set(registry.load(str(stores))) == {"one"}

    os.utime(stores, (0, 0))  # a distinct mtime; content change alone is not enough
    stores.write_text(
        f"stores:\n  - id: two\n    kind: context-store\n"
        f"    paths: {{root: {tmp_path}}}\n",
        encoding="utf-8",
    )
    assert set(registry.load(str(stores))) == {"two"}


@pytest.mark.anyio
async def test_sources_register_in_declared_order_however_they_open(
    tmp_path: Path,
) -> None:
    """Opening is concurrent; registration is not.

    `sources()` documents connection order and `Ref.parse` resolves a bare key against
    `current`, so arrival order is observable behaviour. A source that opens fast must
    not overtake one declared before it.
    """
    for name in ("alpha", "beta", "gamma"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "n.md").write_text("x", encoding="utf-8")

    session = Session()
    failures = await session.connect_many(
        [
            SourceRequest(spec=str(tmp_path / name), kind="files", source_id=name)
            for name in ("alpha", "beta", "gamma")
        ]
    )

    assert failures == []
    assert [d.id for d in session.sources()] == ["alpha", "beta", "gamma"]
    assert session.current == "alpha"  # the first declared, not the first opened
    await session.aclose()


@pytest.mark.anyio
async def test_one_unreachable_source_does_not_stop_the_others(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    session = Session()
    failures = await session.connect_many(
        [
            SourceRequest(spec=str(tmp_path / "nope"), kind="files", source_id="bad"),
            SourceRequest(spec=str(tmp_path / "real"), kind="files", source_id="good"),
        ]
    )

    assert len(failures) == 1 and failures[0].startswith("bad:")
    assert [d.id for d in session.sources()] == ["good"]
    await session.aclose()
