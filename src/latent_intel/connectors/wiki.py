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

**The contract is a published format**, `latent-wiki.store` v1 (the producer's
`docs/formats/wiki-store.md`), stamped in the manifest as `format`/`format_version`. A
store without the stamp, or with a version this build does not know, is refused loudly
rather than misread quietly. The paired context store is `latent-wiki.context` v1,
stamped in its `context-store.yaml`.

**This is the one implementation of the wiki tools.** latent-intel's runtimes call it in
process; `intel serve` exposes the same calls over MCP. Two measured ranking decisions
hold either way:

- **Filter before rank.** Superseded and deprecated pages are excluded by predicate, not
  down-weighted. A retired page and its replacement are near-identical by construction,
  so ranking cannot separate them — and the deprecated one sometimes wins.
- **Rank over the gist**, not the whole body, unless asked: key, title, aliases, tags,
  intent and lead, weighted in that order, through SQLite FTS5 (`_fts.py`).

**Disclosure is a ladder, and every rung is bounded**: overview → index or search →
get (a section, or a capped page) → neighbors and trace → evidence (the context-store
entries a page cites) → raw (the source text, paged). Each answer says where it came
from and what it left out.

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

import yaml
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
from . import _fts

MANIFEST_NAME = "manifest.json"

#: The manifest layout this build reads. A store declaring another version is refused —
#: silently misreading a newer artifact is the failure this exists to prevent.
MANIFEST_VERSION = 1

#: The published formats this build reads. Anything else, or nothing, is refused.
STORE_FORMAT = ("latent-wiki.store", 1)
CONTEXT_FORMAT = ("latent-wiki.context", 1)

#: Caps on what one answer may carry. Each answer that hits one says so.
GET_MAX_CHARS = 8_000
RAW_MAX_CHARS = 12_000
INDEX_PAGE = 50
INDEX_MAX = 200
SEARCH_MAX = 50
NEIGHBORS_MAX = 40
EVIDENCE_MAX = 20
SMALL_TYPE = 12

#: A context-store entry key: one path segment, never a path.
_ENTRY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Lifecycle states search hides unless asked. A predicate, never a ranking penalty.
RETIRED = ("superseded", "deprecated")

#: Where bodies live, relative to the store root.
PAGES_DIR = "pages"

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n?", re.DOTALL)

#: Why an unpublished store could not be read, by what is actually at the root. The
#: three cases have different fixes, and one message for all of them sent a deployment
#: with no local copy — the normal case for a store hosted on S3 — chasing a publish
#: step for a directory that was never there. The remedy named is the source, not a
#: package:
#: a client reads a published store and never needs the tool that wrote one.
NO_STORE = (
    "{root} does not exist, so this machine has no copy of the store. Point the source "
    "at a published store — a path or s3:// URL containing {manifest} — or correct its "
    "target in the project file."
)
EMPTY_STORE = (
    "{root} is empty, so this machine has no copy of the store. If it is a synced "
    "folder the data has not arrived; otherwise point the source at a published store "
    "— a path or s3:// URL containing {manifest} — or correct its target in the "
    "project file."
)
#: A remote read the provider refused. S3 answers 403 for a wrong key, a policy without
#: `s3:GetObject`/`s3:ListBucket` on the prefix, and — without list permission — a key
#: that simply is not there, so the message names all three rather than guessing.
REFUSED = (
    "{path}: {error} — the credentials in the environment were refused. Check the key "
    "(`intel doctor` lists which variables are set), the policy on this prefix, and "
    "that the bucket and path are the ones intended."
)
UNPUBLISHED_STORE = (
    "{root} has no {manifest}, so it is an unpublished store this client cannot read. "
    "Point the source at a published copy, or publish this one with the tool that "
    "built it."
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

    @property
    def aliases(self) -> list[str]:
        return [str(a) for a in (self.raw.get("aliases") or [])]

    @property
    def source_docs(self) -> list[str]:
        """Existence-trigger evidence: page keys or `<root>:<key>` references."""
        return [str(d) for d in (self.raw.get("source_docs") or [])]

    @property
    def evidence(self) -> list[str]:
        """The `source_docs` entries that name another store's entry, not a page."""
        return [d for d in self.source_docs if ":" in d and "://" not in d]

    @property
    def parent(self) -> str:
        return str(self.raw.get("part_of") or "")

    @property
    def draft(self) -> bool:
        return self.status == "draft"

    @property
    def inferred(self) -> bool:
        """A page the store marks as drawn by its author, not stated by a source."""
        return str(self.raw.get("derivation") or "") == "inferred"

    def resources(self) -> list[str]:
        """What a page derives from: declared sources, then cited evidence."""
        declared = [str(s["resource"]) for s in self.sources if s.get("resource")]
        return declared + [e for e in self.evidence if e not in declared]

    def flags(self) -> str:
        marks = [
            m for m, on in (("draft", self.draft), ("inferred", self.inferred)) if on
        ]
        return f" · {' · '.join(marks)}" if marks else ""

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
        #: Index page bodies as well as gists. Off by default: over `s3://` it costs one
        #: GET per page, and the measured benefit is in the gist.
        self.index_body = str(options.get("index_body") or "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        self.manifest: dict[str, Any] = {}
        self.pages: dict[str, _Page] = {}
        self._from_manifest = False
        self._context_checked = False
        self._load()
        self._index: _fts.Index | None = self._build_index()

    def _build_index(self) -> _fts.Index | None:
        """The FTS5 index over this store, or None where sqlite lacks FTS5."""
        if not _fts.available():
            return None
        rows = [
            _fts.Row(
                key,
                {
                    "title": page.title,
                    "aliases": " ".join(page.aliases),
                    "tags": " ".join(page.tags).replace("-", " "),
                    "why": page.why,
                    "lead": page.lead,
                    "body": page.body() if self.index_body else "",
                },
            )
            for key, page in self.pages.items()
        ]
        return _fts.Index(rows)

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
        if "://" in self.target:
            # Read, never pre-check: over fsspec `is_file()` answers False to a 403, a
            # missing bucket and a missing key alike, and the refusal that follows
            # got reported as "does not exist" — sending a client with the wrong key
            # to look for a directory. The read raises the real reason.
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ConnectError(REFUSED.format(path=path, error=exc)) from exc
        else:
            try:
                if not path.is_file():
                    return None
                text = path.read_text(encoding="utf-8")
            except OSError:
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
                f"regenerate the store with the tool that built it"
            )
        name, version = STORE_FORMAT
        if (data.get("format"), data.get("format_version")) != STORE_FORMAT:
            found = f"{data.get('format')!r} v{data.get('format_version')!r}"
            raise ConnectError(
                f"{self.id}: {MANIFEST_NAME} is {found}; this build reads {name} "
                f"v{version}. An unstamped manifest predates the published format — "
                f"republish the store with the tool that built it"
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

    def _unreadable(self) -> str:
        """Which of the three unpublished cases this root is, as a message template.

        A remote root that cannot be listed is reported as missing: an unreachable
        `s3://` prefix and an absent directory are the same fact to the caller, and
        guessing which would be a guess about someone else's network.
        """
        try:
            if not self.root.exists():
                return NO_STORE
            if self.root.is_dir() and not any(self.root.iterdir()):
                return EMPTY_STORE
        except OSError:
            return NO_STORE
        return UNPUBLISHED_STORE

    def _load_library(self) -> None:
        """An unpublished store, through `latent-wiki` if this machine has it.

        Only reachable on an authoring machine: a store with no manifest has not been
        published, and publishing is what writes one.
        """
        try:
            from latent_wiki.store import Store
        except ImportError:
            raise ConnectError(
                self._unreadable().format(root=self.root, manifest=MANIFEST_NAME)
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
                # What the store is *about*, as the manifest states it. Asked the topic
                # of an attached wiki, a model made no tool call and said it could not
                # tell — nothing in its prompt said. A store that declares none
                # gives an empty string, and the prompt prints nothing for it.
                "description": str(self.manifest.get("description") or ""),
                "root": str(self.root),
                "pack": str(self.manifest.get("pack") or ""),
                "store_id": str(self.manifest.get("store_id") or ""),
                "from_manifest": self._from_manifest,
                "context": str(self.context) if self.context else "",
            },
        )

    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        """Rank by FTS5 over the gist fields. Filters run first and are absolute.

        Filters: `type`, `tag`, `part_of`, `status`, a `where` mapping over any
        frontmatter field (a value or a list of accepted values), `include_retired`
        (default off) and `include_drafts` (default on — a draft is shown, flagged).
        A query naming a page exactly — key, title or alias — puts that page first.
        """
        q = str(query).strip()
        if not q:
            return []
        keep = self._filter(filters)
        ranked = self._rank(q, keep)
        lowered = q.lower()
        exact = {
            key
            for key, page in self.pages.items()
            if lowered in {key, page.title.lower(), *(a.lower() for a in page.aliases)}
        }
        ordered = [r for r in ranked if r[0] in exact] + [
            r for r in ranked if r[0] not in exact
        ]
        hits: list[Hit] = []
        for key, score, matched_on in ordered:
            if key not in keep:
                continue
            page = self.pages[key]
            hits.append(
                Hit(
                    ref=f"{self.id}:{key}",
                    source_id=self.id,
                    title=page.title,
                    kind=page.type or "?",
                    lead=page.lead[:280],
                    score=round(score, 3),
                    provenance=self._provenance(key, "search"),
                    metadata={
                        "matched_on": list(matched_on)
                        or (["exact"] if key in exact else []),
                        "draft": page.draft,
                        "inferred": page.inferred,
                        "exact": key in exact,
                    },
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def _rank(
        self, query: str, allowed: set[str]
    ) -> list[tuple[str, float, tuple[str, ...]]]:
        """FTS5 where available; a substring count over the gist where it is not."""
        if self._index is not None:
            ranked = self._index.rank(query, allowed)
            return [(r.key, r.score, r.matched_on) for r in ranked]
        q = query.lower()
        scored = []
        for key, page in self.pages.items():
            haystack = f"{key} {page.title} {page.why} {page.lead}".lower()
            n = haystack.count(q)
            if n:
                scored.append(
                    (float(n + 5 * (q in key) + 3 * (q in page.title.lower())), key)
                )
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [(key, score, ()) for score, key in scored]

    def _filter(self, filters: dict[str, Any]) -> set[str]:
        """The keys a query may return, by predicate. Never a ranking penalty."""
        type_filter = str(filters.get("type") or filters.get("kind") or "")
        tag_filter = str(filters.get("tag") or "")
        parent = str(filters.get("part_of") or "")
        status = str(filters.get("status") or "")
        include_retired = bool(filters.get("include_retired"))
        include_drafts = filters.get("include_drafts", True) not in (False, "false", 0)
        where = filters.get("where") or {}
        if not isinstance(where, dict):
            raise ConnectError(
                "`where` must map a frontmatter field to a value or list"
            )
        keep: set[str] = set()
        for key, page in self.pages.items():
            if type_filter and page.type != type_filter:
                continue
            if tag_filter and tag_filter not in page.tags:
                continue
            if parent and page.parent != parent:
                continue
            if status and page.status != status:
                continue
            if not status and not include_retired and page.status in RETIRED:
                continue
            if not include_drafts and page.draft:
                continue
            if not all(_field_matches(page.raw.get(f), v) for f, v in where.items()):
                continue
            keep.add(key)
        return keep

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
                "source_docs": page.source_docs,
                "part_of": page.parent,
                "draft": page.draft,
                "inferred": page.inferred,
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
        if self.index_body:
            options["index_body"] = "true"
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
        key = str(arguments.get("key") or "")
        if name == "wiki_overview":
            return self.overview()
        if name == "wiki_index":
            return self.index(
                type=str(arguments.get("type") or ""),
                part_of=str(arguments.get("part_of") or ""),
                offset=_clamp(arguments.get("offset"), 0, 0, None),
                limit=_clamp(arguments.get("limit"), INDEX_PAGE, 1, INDEX_MAX),
            )
        if name == "wiki_search":
            query = str(arguments.get("query") or "")
            limit = _clamp(arguments.get("limit"), 10, 1, SEARCH_MAX)
            hits = await self.search(
                query,
                limit=limit + 1,
                type=arguments.get("type") or "",
                tag=arguments.get("tag") or "",
                part_of=arguments.get("part_of") or "",
                status=arguments.get("status") or "",
                where=arguments.get("where") or {},
                include_retired=bool(arguments.get("include_retired")),
                include_drafts=arguments.get("include_drafts", True),
            )
            if not hits:
                return f"no matches for {query!r}" + self._footer("search", query)
            more = len(hits) > limit
            hits = hits[:limit]
            lines = [
                f"[{h.kind}] {h.ref} — {h.title}{self._flags(h)}\n"
                f"  {h.lead}\n"
                f"  matched on: {', '.join(h.metadata['matched_on']) or '—'}"
                for h in hits
            ]
            cut = f"first {limit}; raise limit or filter" if more else ""
            return "\n\n".join(lines) + self._footer("search", query, cut=cut)
        if name == "wiki_get":
            return self.get(
                key,
                section=str(arguments.get("section") or ""),
                offset=_clamp(arguments.get("offset"), 0, 0, None),
            )
        if name == "wiki_neighbors":
            return self._neighbors_text(key)
        if name == "wiki_trace":
            return self._trace_text(key)
        if name == "wiki_evidence":
            return self.evidence(
                key,
                entry=str(arguments.get("entry") or ""),
                offset=_clamp(arguments.get("offset"), 0, 0, None),
            )
        if name == "wiki_raw":
            return self.raw(
                key,
                _clamp(arguments.get("offset"), 0, 0, None),
                _clamp(arguments.get("max_chars"), RAW_MAX_CHARS, 1, RAW_MAX_CHARS),
            )
        raise ConnectError(f"{self.id} has no tool '{name}'")

    async def aclose(self) -> None:
        return None

    # -- the ladder -----------------------------------------------------------

    def overview(self) -> str:
        """The one routing rung: what this store holds and where to go next."""
        by_type: dict[str, list[_Page]] = {}
        for page in self.pages.values():
            by_type.setdefault(page.type or "?", []).append(page)
        drafts = sum(page.draft for page in self.pages.values())
        retired = sum(page.status in RETIRED for page in self.pages.values())
        lines = [
            str(self.manifest.get("title") or self.id),
            str(self.manifest.get("description") or "").strip(),
            "",
            f"{len(self.pages)} pages ({drafts} draft, {retired} retired):",
        ]
        for kind, pages in sorted(by_type.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"  {kind}: {len(pages)}")
        children: dict[str, int] = {}
        for page in self.pages.values():
            if page.parent:
                children[page.parent] = children.get(page.parent, 0) + 1
        roots = sorted(
            (k for k in children if k in self.pages and not self.pages[k].parent),
            key=lambda k: self.pages[k].title,
        )
        if roots:
            lines += [
                "",
                "Structures (part_of trees) — wiki_index part_of=<key>, or a view:",
            ]
            lines += [
                f"  {k} — {self.pages[k].title} ({_descendants(self.pages, k)} beneath)"
                for k in roots
            ]
        small = [kind for kind, pages in by_type.items() if len(pages) <= SMALL_TYPE]
        for kind in sorted(small):
            lines += ["", f"All {kind} pages:"]
            lines += [
                f"  {p.key} — {p.title}{p.flags()}"
                for p in sorted(by_type[kind], key=lambda p: p.key)
            ]
        lines += [
            "",
            "Next: wiki_search for a topic, wiki_index type=<type> to list, "
            "wiki_get for a "
            "page, wiki_evidence for what a page rests on.",
        ]
        return "\n".join(lines) + self._footer("overview", "")

    def index(
        self,
        *,
        type: str = "",
        part_of: str = "",
        offset: int = 0,
        limit: int = INDEX_PAGE,
    ) -> str:
        """Every page as one line, paged: of one type, or one parent's children."""
        chosen = [
            p
            for p in self.pages.values()
            if (not type or p.type == type) and (not part_of or p.parent == part_of)
        ]
        pages = sorted(chosen, key=lambda p: (_step(p) if part_of else 0, p.key))
        chunk = pages[offset : offset + limit]
        lines = [f"[{p.type}] {p.key} — {p.title}{p.flags()}\n  {p.why}" for p in chunk]
        end = offset + len(chunk)
        cut = f"{end} of {len(pages)}; next offset={end}" if end < len(pages) else ""
        subject = part_of or type or "all"
        return "\n".join(lines) + self._footer("index", subject, cut=cut)

    def get(self, key: str, *, section: str = "", offset: int = 0) -> str:
        """One page: header and body, a named section of it, capped either way."""
        page = self.pages.get(key)
        if page is None:
            return f"no page '{key}' in {self.id}"
        body = page.body().strip()
        headings = [text for _, text in _headings(body)]
        if section:
            body = _section(body, section)
            if not body:
                return (
                    f"no section {section!r} in {key}; sections: "
                    f"{'; '.join(headings) or '(none)'}"
                )
        text = body[offset : offset + GET_MAX_CHARS]
        end = offset + len(text)
        header = [f"{page.title}  [{page.type}{page.flags()}]", f"why: {page.why}"]
        if page.parent:
            header.append(f"part of: {page.parent}")
        if page.evidence:
            header.append(
                f"evidence: {len(page.evidence)} entries — wiki_evidence key={key}"
            )
        if headings and not section:
            header.append(f"sections: {'; '.join(headings)}")
        cut = ""
        if end < len(body):
            cut = f"{end} of {len(body)} chars; call again with offset={end}"
        return "\n".join(header) + "\n\n" + text + self._footer("get", key, cut=cut)

    def evidence(self, key: str, *, entry: str = "", offset: int = 0) -> str:
        """What a page rests on, from the paired context store.

        Without `entry`: one gist per cited entry — its title, intent and lead. With
        `entry`: that entry in full, paged. Either way, the next rung is `wiki_raw`.
        """
        page = self.pages.get(key)
        if page is None and not entry:
            return f"no page '{key}' in {self.id}"
        problem = self._check_context()
        if problem:
            return problem
        if entry:
            path = self._summary_path(entry)
            if path is None:
                return f"no context entry '{entry}' under {self.context}"
            loaded = _read_entry(path)
            if isinstance(loaded, str):
                return loaded
            meta, text = loaded
            chunk = text[offset : offset + RAW_MAX_CHARS]
            end = offset + len(chunk)
            cut = f"{end} of {len(text)}; offset={end}" if end < len(text) else ""
            return (
                f"{meta.get('title') or entry}\n{meta.get('source') or ''}\n\n{chunk}"
                + self._footer("evidence", _bare(entry), cut=cut)
                + f"\nNext: wiki_raw key={_bare(entry)} for the source text."
            )
        assert page is not None
        if not page.evidence:
            return f"{key} cites no context-store entries" + self._footer(
                "evidence", key
            )
        lines = []
        cited = page.evidence
        for ref in cited[:EVIDENCE_MAX]:
            path = self._summary_path(ref)
            if path is None:
                lines.append(f"- {ref}: not found in {self.context}")
                continue
            loaded = _read_entry(path)
            if isinstance(loaded, str):
                lines.append(f"- {ref}: {loaded}")
                continue
            meta, text = loaded
            lead = text.strip().split("\n\n", 1)[0][:280]
            lines.append(
                f"- {_bare(ref)} — {meta.get('title') or ''}\n"
                f"  {meta.get('source') or ''}\n"
                f"  why: {meta.get('why') or ''}\n  {lead}"
            )
        cut = (
            f"{EVIDENCE_MAX} of {len(cited)} entries; pass entry=<key> for the others"
            if len(cited) > EVIDENCE_MAX
            else ""
        )
        return (
            "\n".join(lines)
            + self._footer("evidence", key, cut=cut)
            + "\nNext: wiki_evidence entry=<key> for one in full, "
            "wiki_raw key=<key> for its source."
        )

    def raw(self, key: str, offset: int = 0, max_chars: int = RAW_MAX_CHARS) -> str:
        """Escalate to source text, paged — the last rung, and it must be bounded.

        `key` may be a page — its declared sources first, then the first cited
        evidence entry's `raw_path` — or a context-store entry, by key.
        """
        page = self.pages.get(key)
        candidates: list[tuple[str, Path | None]] = []
        if page is not None:
            for resource in page.resources():
                if resource in page.evidence:
                    candidates.append((resource, self._raw_of_entry(resource)))
                else:
                    candidates.append((resource, self._resolve_origin(resource)))
        elif self.context is not None:
            candidates.append((key, self._raw_of_entry(key)))
        else:
            return f"no page '{key}'"
        for label, path in candidates:
            if path is None:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, FileNotFoundError):
                continue
            chunk = text[offset : offset + max_chars]
            more = offset + max_chars < len(text)
            head = f"{label}  [{offset}:{offset + len(chunk)} of {len(text)}]"
            tail = (
                f"\n\n… more: call again with offset={offset + max_chars}"
                if more
                else ""
            )
            return f"{head}\n\n{chunk}{tail}"
        declared = ", ".join(page.resources()) if page else key
        if self.context is None:
            return (
                f"no source attached for '{key}' (declared: {declared or 'none'}). "
                f"Pair this wiki with its material: `context:` on the source."
            )
        return (
            f"no readable source for '{key}' (declared: {declared or 'none'}) under "
            f"{self.context}. Remote or missing sources cannot be paged — this is not "
            f"an empty source."
        )

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
        """Where a page came from and where it sits: evidence, lifecycle, parents."""
        page = self.pages.get(key)
        if page is None:
            return {}
        chain, seen, at = [], {key}, page.parent
        while at and at not in seen and at in self.pages:
            chain.append(at)
            seen.add(at)
            at = self.pages[at].parent
        return {
            "key": key,
            "sources": page.sources,
            "source_docs": page.source_docs,
            "part_of": " < ".join(chain),
            "children": sorted(k for k, p in self.pages.items() if p.parent == key),
            "provenance": page.raw.get("provenance") or {},
            "captured": str(page.raw.get("captured") or ""),
            "as_of": str(page.raw.get("as_of") or ""),
            "status": page.status,
            "supersedes": page.raw.get("supersedes") or [],
            "superseded_by": page.raw.get("superseded_by") or "",
        }

    def _neighbors_text(self, key: str) -> str:
        edges = self.neighbors(key)
        if not edges:
            return f"no page '{key}'"
        parts, cut = [], []
        for direction, arrow in (("out", "->"), ("in", "<-")):
            rows = edges[direction]
            shown = rows[:NEIGHBORS_MAX]
            body = "\n".join(
                f"  {arrow} {t} — {self._title(t)}  ({d})" for t, d in shown
            )
            parts.append(f"{direction}:\n{body or '  (none)'}")
            if len(rows) > len(shown):
                cut.append(f"{direction} {len(shown)} of {len(rows)}")
        return "\n".join(parts) + self._footer("neighbors", key, cut=", ".join(cut))

    def _trace_text(self, key: str) -> str:
        trace = self.trace(key)
        if not trace:
            return "no page"
        return "\n".join(f"{k}: {v}" for k, v in trace.items() if v) + self._footer(
            "trace", key
        )

    # -- internals ------------------------------------------------------------

    def _check_context(self) -> str:
        """Why the paired context store cannot be read, or "" when it can."""
        if self.context is None:
            return (
                "no context store paired with this wiki — set `context:` on the source "
                "to the store its pages cite."
            )
        if self._context_checked:
            return ""
        descriptor = self.context / "context-store.yaml"
        try:
            data = yaml.safe_load(descriptor.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return (
                f"{descriptor} is missing — the paired context store is not published"
            )
        except OSError as exc:
            return REFUSED.format(path=descriptor, error=exc)
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            return f"{descriptor} cannot be read as YAML: {exc}"
        found = (
            (data.get("format"), data.get("format_version"))
            if isinstance(data, dict)
            else None
        )
        if found != CONTEXT_FORMAT:
            name, version = CONTEXT_FORMAT
            return (
                f"{descriptor} declares {found}; this build reads {name} v{version} — "
                f"republish the context store with the tool that built it"
            )
        self._context_checked = True
        return ""

    def _summary_path(self, ref: str) -> Path | None:
        key = _bare(ref)
        if self.context is None or not _ENTRY_KEY.match(key) or ".." in key:
            return None
        candidate = self.context / "summaries" / f"{key}.md"
        try:
            return candidate if candidate.is_file() else None
        except OSError:
            return None

    def _raw_of_entry(self, ref: str) -> Path | None:
        """A context entry's `raw_path`, resolved against the context root."""
        path = self._summary_path(ref)
        if path is None or self.context is None:
            return None
        loaded = _read_entry(path)
        if isinstance(loaded, str):
            return None
        raw_path = str(loaded[0].get("raw_path") or "")
        if not raw_path or _UNSAFE_RAW.search(raw_path):
            return None
        candidate = self.context / raw_path.rstrip("/")
        try:
            if candidate.is_file():
                return candidate
            if candidate.is_dir():
                files = sorted(f for f in candidate.iterdir() if f.suffix == ".md")
                return files[0] if files else None
        except OSError:
            return None
        return None

    def _resolve_origin(self, resource: str) -> Path | None:
        """Map a `sources[].resource` to something readable, or None.

        A resource is written `corpus:raw/paper.md` — a root name the store declares,
        then a path under it. The paired `context:` is that root: one location, named
        once in the project file, rather than a wiki config this client cannot see.
        """
        if not resource or "://" in resource:
            return None
        _, _, rest = resource.rpartition(":")
        if not rest or _UNSAFE_RAW.search(rest):
            return None
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

    def _footer(self, rung: str, subject: str, *, cut: str = "") -> str:
        """Where an answer came from and what it left out — on every answer."""
        generated = str(self.manifest.get("generated") or "")
        where = f"{self.id}:{subject}" if subject else self.id
        line = f"\n\n— {rung} · {where}" + (
            f" · store as of {generated}" if generated else ""
        )
        return line + f"\n  cut: {cut or 'nothing'}"

    def _title(self, key: str) -> str:
        page = self.pages.get(key)
        return page.title if page else "?"

    @staticmethod
    def _flags(hit: Hit) -> str:
        marks = [m for m in ("draft", "inferred") if hit.metadata.get(m)]
        return f" · {' · '.join(marks)}" if marks else ""


#: A `raw_path` a reader can follow: relative, inside the context store.
_UNSAFE_RAW = re.compile(
    r"^[/\\~]|^[A-Za-z]:|^[A-Za-z][A-Za-z0-9+.-]*://|(^|[/\\])\.\.([/\\]|$)"
)


def _bare(ref: str) -> str:
    """`corpus:<key>` → `<key>`; a bare key stays as it is."""
    return ref.rsplit(":", 1)[-1]


def _read_entry(path: Path) -> tuple[dict[str, Any], str] | str:
    """A context entry's frontmatter and body, read once — or why it could not be."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return f"{path.name} is not UTF-8 text ({exc.reason})"
    except OSError as exc:
        return f"{path.name} could not be read: {exc}"
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        parsed = yaml.safe_load(match.group(0).strip().strip("-")) or {}
    except yaml.YAMLError as exc:
        return f"{path.name} has frontmatter that is not YAML: {exc}"
    return (parsed if isinstance(parsed, dict) else {}), text[match.end() :]


def _clamp(value: Any, default: int, low: int, high: int | None) -> int:
    """A caller's number, defaulted and held inside the bounds every rung promises."""
    try:
        number = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        number = default
    number = max(low, number)
    return min(number, high) if high is not None else number


def _step(page: _Page) -> int:
    step = page.raw.get("step")
    return step if isinstance(step, int) else 10**6


_HEADING = re.compile(r"^(#{1,3}) +(.+?) *$")


def _headings(body: str) -> list[tuple[int, str]]:
    """`(line index, text)` of each `##`/`###` heading outside fenced code."""
    found, fenced = [], False
    for i, line in enumerate(body.splitlines()):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        m = _HEADING.match(line)
        if not fenced and m and len(m.group(1)) >= 2:
            found.append((i, m.group(2)))
    return found


def _section(body: str, name: str) -> str:
    """One `##`/`###` section, by heading text (case-insensitive prefix).

    It runs to the next heading of the same or a higher level — a `#` heading ends it
    too — and headings inside fenced code are text, not structure.
    """
    lines = body.splitlines()
    want = name.lower().strip()
    start, level, fenced = None, 0, False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        m = _HEADING.match(line)
        if fenced or not m:
            continue
        depth = len(m.group(1))
        if start is None:
            if depth >= 2 and m.group(2).lower().startswith(want):
                start, level = i, depth
        elif depth <= level:
            return "\n".join(lines[start:i]).strip()
    return "\n".join(lines[start:]).strip() if start is not None else ""


def _descendants(pages: dict[str, _Page], key: str) -> int:
    kids = [k for k, p in pages.items() if p.parent == key]
    return len(kids) + sum(_descendants(pages, k) for k in kids)


def _field_matches(value: Any, wanted: Any) -> bool:
    """A frontmatter value against a `where` filter value or list of values."""
    accepted = {str(w) for w in wanted} if isinstance(wanted, list) else {str(wanted)}
    if isinstance(value, list):
        return any(str(v) in accepted for v in value)
    return str(value) in accepted if value is not None else False


_KEY_ONLY: dict[str, Any] = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
}

# Descriptions are prompts. Each one says what the tool cannot do as well as what it
# can, and where to go next — an overpromising description produces confident wrong
# answers, and a ladder nobody can see is not climbed. A parameter whose default is not
# the empty value declares it: over MCP an omitted argument arrives as that default,
# and `include_drafts: false` or `limit: 0` would quietly change the answer.
_TOOLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "wiki_overview",
        "Start here. What this wiki holds: page counts by type, the structures "
        "(part_of trees) in it, and every page of the small types. One screen.",
        {"type": "object", "properties": {}},
    ),
    (
        "wiki_index",
        "List pages one line each (title and intent), paged: of one type, or the "
        "children of one page in step order (part_of). Use to browse; use wiki_search "
        "to find something.",
        {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "part_of": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer", "default": INDEX_PAGE},
            },
        },
    ),
    (
        "wiki_search",
        "Full-text search over key, title, aliases, tags, intent and lead (stemmed, "
        "weighted in that order). Filters apply before ranking: type, tag, part_of, "
        "status, and `where` over any frontmatter field. Superseded pages are hidden "
        "unless include_retired; drafts are shown and flagged. Returns gists and what "
        "each matched on — use wiki_get for a page.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string"},
                "tag": {"type": "string"},
                "part_of": {"type": "string"},
                "status": {"type": "string"},
                "where": {"type": "object"},
                "include_retired": {"type": "boolean", "default": False},
                "include_drafts": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    ),
    (
        "wiki_get",
        "One page: type, intent, parent, its section headings, then the body — capped, "
        "with an offset to continue. Pass `section` for one heading only.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "section": {"type": "string"},
                "offset": {"type": "integer"},
            },
            "required": ["key"],
        },
    ),
    (
        "wiki_neighbors",
        "What a page links to and what links back, with each edge's derivation "
        "(extracted, inferred or ambiguous). Capped.",
        _KEY_ONLY,
    ),
    (
        "wiki_trace",
        "Provenance and position: cited evidence, sources, authorship, lifecycle, the "
        "chain of parents (part_of) and the direct children.",
        _KEY_ONLY,
    ),
    (
        "wiki_evidence",
        "What a page rests on: one gist per context-store entry it cites (title, "
        "pages, intent, lead). Pass `entry` for one entry in full. Needs the wiki "
        "paired with its context store.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "entry": {"type": "string"},
                "offset": {"type": "integer"},
            },
            "required": ["key"],
        },
    ),
    (
        "wiki_raw",
        "The source text behind a page or a context entry, paged. The last rung: use "
        "when the page and its evidence do not answer, and quote from it.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "offset": {"type": "integer"},
                "max_chars": {"type": "integer", "default": RAW_MAX_CHARS},
            },
            "required": ["key"],
        },
    ),
)
