"""A `latent-wiki` store, read through its **published format** — local or `s3://`.

A published store is a versioned artifact, not a library secret::

    manifest.json    manifest_version, contract_version, store_id, pack, title,
                     description, generated, page_count,
                     pages: [{key, file, lead, frontmatter}]      one GET
    pages/<file>.md  bodies, fetched only when something asks     on demand

Reading that format needs `json` and `fsspec`, both of which the base install already
has. So **connecting to an already-built wiki requires no optional dependency**: install
the client, point a project at `s3://…`, and it works. `latent-wiki` is what you install
to *build* a wiki, and this connector falls back to it only for a store that was never
published — a working directory with no `manifest.json`, which is an authoring machine
by definition.

Two ranking decisions are reproduced here from `latent_wiki.serve`, deliberately and
with the reason attached, because they took measurement to reach:

- **Filter before rank.** Superseded and deprecated pages are excluded by predicate, not
  down-weighted. A retired page and its replacement are near-identical by construction,
  so ranking cannot separate them — and the deprecated one sometimes wins.
- **Rank over the lead paragraph**, not the whole body. The one preregistered ablation
  on a store of this shape found retrieval over entry gists carried the benefit.

Duplicating them is the price of not depending on the producer, and the guard against
drift is `manifest_version`: a store whose format this build does not read is refused
loudly rather than misread quietly.

**Field vocabulary is the default pattern** — `type`, `title`, `why`, `status`, `tags`,
`links`, `sources`, `as_of` — which is what the packs we ship write. A store with a
different vocabulary is a sidecar mapping away; the seam is `_Page`, which is the only
place frontmatter keys are named.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from upath import UPath

from ..models import (
    Capability,
    ConnectError,
    Descriptor,
    Doc,
    Effect,
    Hit,
    Provenance,
    ToolSpec,
)

MANIFEST_NAME = "manifest.json"

#: The manifest layout this build reads. A store declaring another version is refused —
#: silently misreading a newer artifact is the failure this exists to prevent.
MANIFEST_VERSION = 1

#: Lifecycle states search hides unless asked. A predicate, never a ranking penalty.
RETIRED = ("superseded", "deprecated")

#: Where bodies live, relative to the store root.
PAGES_DIR = "pages"

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n?", re.DOTALL)

MISSING_MANIFEST = (
    "{root} has no {manifest} and `latent_wiki` is not installed. "
    "Publish the store with `lw manifest` (it is what `just publish` runs), or add the "
    "package by path:  uv tool install . --with ../latent-wiki --reinstall"
)


def _as_path(target: str) -> Path:
    """A local path or any fsspec URI. `UPath` implements everything used here."""
    if "://" in target:
        return cast(Path, UPath(target))
    return Path(target).expanduser().resolve()


@dataclass
class _Page:
    """One page at the scan and gist rungs. **The only place frontmatter is named.**

    A store with a different field vocabulary is a mapping over this class, which is why
    every accessor below reads `raw` rather than being handed a value.
    """

    key: str
    raw: dict[str, Any]
    lead: str = ""
    #: Where the body lives. `None` for a page whose body a library loader supplies.
    path: Path | None = None
    _library_page: Any = field(default=None, repr=False)

    @property
    def title(self) -> str:
        return str(self.raw.get("title") or self.key)

    @property
    def type(self) -> str:
        return str(self.raw.get("type") or "")

    @property
    def why(self) -> str:
        return str(self.raw.get("why") or "")

    @property
    def status(self) -> str:
        return str(self.raw.get("status") or "")

    @property
    def tags(self) -> list[str]:
        return [str(t) for t in (self.raw.get("tags") or [])]

    @property
    def links(self) -> list[dict[str, str]]:
        """`links:` entries, as `{to, rel, derivation}`. A bare string is a link too."""
        out: list[dict[str, str]] = []
        for entry in self.raw.get("links") or []:
            if isinstance(entry, str):
                out.append({"to": entry, "rel": "", "derivation": "inferred"})
            elif isinstance(entry, dict) and entry.get("to"):
                out.append(
                    {
                        "to": str(entry["to"]),
                        "rel": str(entry.get("rel") or ""),
                        "derivation": str(entry.get("derivation") or ""),
                    }
                )
        return out

    @property
    def sources(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for entry in self.raw.get("sources") or []:
            if isinstance(entry, str):
                entries.append({"resource": entry})
            elif isinstance(entry, dict):
                entries.append(dict(entry))
        return entries

    def resources(self) -> list[str]:
        return [str(s["resource"]) for s in self.sources if s.get("resource")]

    def body(self) -> str:
        """The page body. This is the fetch a manifest exists to defer."""
        if self._library_page is not None:
            return str(self._library_page.body)
        if self.path is None:
            return ""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError) as exc:
            raise ConnectError(f"{self.key}: {exc}") from exc
        return _FRONTMATTER.sub("", text)


class WikiConnector:
    """One `latent-wiki` store. One connector, one store — keys stay unambiguous."""

    kind = "wiki"

    def __init__(self, source_id: str, target: str, **options: Any) -> None:
        self.id = source_id
        self.target = str(target)
        self.root = _as_path(self.target)
        #: The material pages derive from, for `wiki_raw`. A wiki and its context are a
        #: pair: the wiki carries the summaries, this carries what they summarise.
        context = options.get("context") or options.get("raw")
        self.context: Path | None = _as_path(str(context)) if context else None

        self.manifest: dict[str, Any] = {}
        self.pages: dict[str, _Page] = {}
        self._from_manifest = False
        self._load()

    # -- loading --------------------------------------------------------------

    def _load(self) -> None:
        data = self._read_manifest()
        if data is not None:
            self.manifest = data
            self.pages = self._pages_from_manifest(data)
            self._from_manifest = True
            return
        self._load_library()

    def _read_manifest(self) -> dict[str, Any] | None:
        """The store's manifest, or None when it has none.

        A malformed or wrong-version manifest raises rather than falling back to the
        library: a store that ships a broken artifact should say so, not degrade into a
        path that happens to work on the machine that built it.
        """
        path = self.root / MANIFEST_NAME
        try:
            if not path.is_file():
                return None
            text = path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectError(
                f"{self.id}: {MANIFEST_NAME} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict) or "pages" not in data:
            raise ConnectError(f"{self.id}: {MANIFEST_NAME} is not a manifest")
        declared = int(data.get("manifest_version", MANIFEST_VERSION))
        if declared != MANIFEST_VERSION:
            raise ConnectError(
                f"{self.id}: {MANIFEST_NAME} declares manifest_version {declared}; "
                f"this build reads {MANIFEST_VERSION} — upgrade latent-intel, or "
                f"regenerate the store with `lw manifest`"
            )
        return data

    def _pages_from_manifest(self, data: dict[str, Any]) -> dict[str, _Page]:
        pages_dir = self.root / PAGES_DIR
        pages: dict[str, _Page] = {}
        for entry in data.get("pages") or []:
            key = str(entry.get("key") or "")
            raw = entry.get("frontmatter") or {}
            if not key or not isinstance(raw, dict):
                continue
            pages[key] = _Page(
                key=key,
                raw=raw,
                lead=str(entry.get("lead") or ""),
                path=pages_dir / str(entry.get("file") or f"{key}.md"),
            )
        return pages

    def _load_library(self) -> None:
        """An unpublished store, through `latent-wiki` if this machine has it.

        Only reachable on an authoring machine: a store with no manifest has not been
        published, and publishing is what writes one.
        """
        try:
            from latent_wiki.store import Store
        except ImportError:
            raise ConnectError(
                MISSING_MANIFEST.format(root=self.root, manifest=MANIFEST_NAME)
            ) from None

        from latent_wiki.errors import LatentWikiError

        try:
            store = Store.load(self.root)
        except LatentWikiError as exc:
            raise ConnectError(f"{self.id}: {exc}") from exc
        self.manifest = {
            "store_id": store.store_id or "",
            "pack": store.pack.name,
            "title": store.title,
            "description": store.description,
        }
        self.pages = {
            key: _Page(key=key, raw=page.raw, lead=page.lead(), _library_page=page)
            for key, page in store.pages.items()
        }

    # -- the contract ---------------------------------------------------------

    def describe(self) -> Descriptor:
        return Descriptor(
            id=self.id,
            kind=self.kind,
            title=str(
                self.manifest.get("title") or self.manifest.get("store_id") or self.id
            ),
            capabilities=[Capability.SEARCH, Capability.FETCH, Capability.TOOLS],
            count=len(self.pages),
            unit="pages",
            freshness=str(self.manifest.get("generated") or "") or None,
            detail={
                "root": str(self.root),
                "pack": str(self.manifest.get("pack") or ""),
                "store_id": str(self.manifest.get("store_id") or ""),
                "from_manifest": self._from_manifest,
                "context": str(self.context) if self.context else "",
            },
        )

    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        """Rank over key, title, intent and lead. Filters run first and are absolute."""
        q = str(query).lower().strip()
        if not q:
            return []
        type_filter = str(filters.get("type") or filters.get("kind") or "")
        tag_filter = str(filters.get("tag") or "")
        include_retired = bool(filters.get("include_retired"))

        scored: list[tuple[int, str, _Page]] = []
        for key, page in self.pages.items():
            if type_filter and page.type != type_filter:
                continue
            if tag_filter and tag_filter not in page.tags:
                continue
            if not include_retired and page.status in RETIRED:
                continue
            haystack = f"{key} {page.title} {page.why} {page.lead}".lower()
            n = haystack.count(q)
            if not n:
                continue
            # A match in the key or title says more about what a page *is* than the
            # same match buried in prose.
            score = (
                n
                + 5 * (q in key.lower())
                + 3 * (q in page.title.lower())
                + 2 * (q in page.why.lower())
            )
            scored.append((score, key, page))

        scored.sort(key=lambda row: (-row[0], row[1]))
        return [
            Hit(
                ref=f"{self.id}:{key}",
                source_id=self.id,
                title=page.title,
                kind=page.type or "?",
                lead=page.lead[:280],
                score=float(score),
                provenance=self._provenance(key, "search"),
            )
            for score, key, page in scored[:limit]
        ]

    async def fetch(self, key: str) -> Doc:
        page = self.pages.get(key)
        if page is None:
            raise ConnectError(f"no page '{key}' in {self.id}")
        return Doc(
            ref=f"{self.id}:{key}",
            source_id=self.id,
            title=page.title,
            # Reaching for the body is what triggers the deferred fetch.
            body=page.body().strip(),
            kind=page.type,
            metadata={
                "why": page.why,
                "status": page.status,
                "links": page.links,
                "sources": page.sources,
            },
            provenance=self._provenance(key, "fetch"),
        )

    def server_spec(self) -> dict[str, Any] | None:
        """How a subprocess agent reaches this store: our own server, over stdio.

        This used to be `shutil.which("lw")`, which made agent access depend on a
        *different* package's console script being on `PATH`. That failed silently and
        invisibly — the config was right, `search` and `fetch` worked, and only `ask`
        quietly answered without the source. It fired in practice: `uv tool install`
        links only the main package's entry points, so `lw` sat in the tool environment
        unreachable by name.

        This connector already reads the store; `serve` exposes those same reads. The
        launcher is the running interpreter, so it is correct by construction.
        """
        from .. import serve

        options = {"context": str(self.context)} if self.context else {}
        return serve.launch_spec("wiki", str(self.target), self.id, options)

    def tools(self) -> list[ToolSpec]:
        """Read-only, every one of them.

        `latent-wiki` writes through `validate` / `links --close` / `index`, so a page
        written from here would land unvalidated with its back-links unclosed. The
        absence of a write tool is the design, not an omission.
        """
        return [
            ToolSpec(
                name=name,
                source_id=self.id,
                description=description,
                effect=Effect.EXTERNAL_READ,
                input_schema=schema,
            )
            for name, description, schema in _TOOLS
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "wiki_search":
            hits = await self.search(
                str(arguments.get("query") or ""),
                limit=int(arguments.get("limit") or 10),
                type=arguments.get("type") or "",
                tag=arguments.get("tag") or "",
            )
            if not hits:
                return f"no matches for {arguments.get('query')!r}"
            return "\n\n".join(
                f"[{h.kind}] {h.ref} — {h.title}\n  {h.lead}" for h in hits
            )
        if name == "wiki_get":
            doc = await self.fetch(str(arguments.get("key") or ""))
            return f"{doc.title}\n\n{doc.body}"
        if name == "wiki_neighbors":
            key = str(arguments.get("key") or "")
            edges = self.neighbors(key)
            if not edges:
                return f"no page '{key}'"
            out = "\n".join(f"  -> {t}  ({d})" for t, d in edges["out"]) or "  (none)"
            inn = "\n".join(f"  <- {t}  ({d})" for t, d in edges["in"]) or "  (none)"
            return f"out:\n{out}\nin:\n{inn}"
        if name == "wiki_trace":
            trace = self.trace(str(arguments.get("key") or ""))
            return "\n".join(f"{k}: {v}" for k, v in trace.items() if v) or "no page"
        if name == "wiki_raw":
            return self.raw(
                str(arguments.get("key") or ""),
                int(arguments.get("offset") or 0),
                int(arguments.get("max_chars") or 12_000),
            )
        raise ConnectError(f"{self.id} has no tool '{name}'")

    async def aclose(self) -> None:
        return None

    # -- the graph ------------------------------------------------------------

    def neighbors(self, key: str) -> dict[str, list[tuple[str, str]]]:
        """Outbound and inbound edges, each with its derivation."""
        page = self.pages.get(key)
        if page is None:
            return {}
        out = [(link["to"], link["derivation"]) for link in page.links]
        inn = [
            (other, link["derivation"])
            for other, candidate in self.pages.items()
            for link in candidate.links
            if link["to"] == key
        ]
        return {"out": sorted(out), "in": sorted(inn)}

    def trace(self, key: str) -> dict[str, Any]:
        """Where a page came from: its sources, how it was authored, its lifecycle."""
        page = self.pages.get(key)
        if page is None:
            return {}
        return {
            "key": key,
            "sources": page.sources,
            "provenance": page.raw.get("provenance") or {},
            "captured": str(page.raw.get("captured") or ""),
            "as_of": str(page.raw.get("as_of") or ""),
            "status": page.status,
            "supersedes": page.raw.get("supersedes") or [],
            "superseded_by": page.raw.get("superseded_by") or "",
        }

    def raw(self, key: str, offset: int = 0, max_chars: int = 12_000) -> str:
        """Escalate to a page's underlying source, paged.

        The last rung of progressive disclosure, and the one that has to be bounded — a
        source can be a whole book, and returning it whole would blow the window it was
        meant to save. Reachable only when the source is attached: a wiki carries the
        summaries, and `context:` says where what they summarise lives.
        """
        page = self.pages.get(key)
        if page is None:
            return f"no page '{key}'"
        for resource in page.resources():
            path = self._resolve_origin(resource)
            if path is None:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, FileNotFoundError):
                continue
            chunk = text[offset : offset + max_chars]
            more = offset + max_chars < len(text)
            head = f"{resource}  [{offset}:{offset + len(chunk)} of {len(text)}]"
            tail = (
                f"\n\n… more: call again with offset={offset + max_chars}"
                if more
                else ""
            )
            return f"{head}\n\n{chunk}{tail}"
        declared = ", ".join(page.resources()) or "none"
        if self.context is None:
            return (
                f"no source attached for '{key}' (declared: {declared}). "
                f"Pair this wiki with its material: `context:` on the source."
            )
        return (
            f"no readable source for '{key}' (declared: {declared}) under "
            f"{self.context}. Remote or missing sources cannot be paged — this is not "
            f"an empty source."
        )

    # -- internals ------------------------------------------------------------

    def _resolve_origin(self, resource: str) -> Path | None:
        """Map a `sources[].resource` to something readable, or None.

        A resource is written `corpus:raw/paper.md` — a root name the store declares,
        then a path under it. The paired `context:` is that root: one location, named
        once in the project file, rather than a wiki config this client cannot see.
        """
        if not resource or resource.startswith(("http://", "https://")):
            return None
        _, _, rest = resource.rpartition(":")
        candidates: list[Path] = []
        if self.context is not None:
            candidates += [self.context / rest, self.context / resource]
        candidates += [self.root / rest, self.root / PAGES_DIR / rest]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:  # an unreadable mount is a miss, not a crash
                continue
        return None

    def _provenance(self, key: str, method: str) -> Provenance:
        """Origins come from the page, so a claim traces to material, not the wiki."""
        page = self.pages.get(key)
        resources = page.resources() if page else []
        return Provenance(
            source_id=self.id,
            method=method,
            ref=f"{self.id}:{key}",
            origin=resources[0] if resources else None,
            as_of=(str(page.raw.get("as_of") or "") or None) if page else None,
        )


_KEY_ONLY: dict[str, Any] = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
}

# Descriptions are prompts. Each one says what the tool cannot do as well as what it
# can — an overpromising description produces confident wrong answers.
_TOOLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "wiki_search",
        "Search page keys, titles, intent and lead paragraphs. Filters apply before "
        "ranking; superseded pages are excluded unless include_retired is set. "
        "Returns gists, not bodies — use wiki_get for a page.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string"},
                "tag": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    ("wiki_get", "One page in full: intent plus body.", _KEY_ONLY),
    (
        "wiki_neighbors",
        "What a page links to and what links back, with each edge's derivation "
        "(extracted, inferred or ambiguous).",
        _KEY_ONLY,
    ),
    (
        "wiki_trace",
        "Provenance: what this page derives from, who wrote it, its lifecycle.",
        _KEY_ONLY,
    ),
    (
        "wiki_raw",
        "Read a page's underlying source, paged. Use only when the page says the "
        "detail lives there. Available when the wiki is paired with its context.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "offset": {"type": "integer"},
                "max_chars": {"type": "integer"},
            },
            "required": ["key"],
        },
    ),
)
