"""A plain directory of markdown — the base-install connector.

The simplest thing that is genuinely useful, and the one that ships with no optional
dependency. It exists for two reasons beyond its own utility:

**Raw material is reachable without a wiki.** A directory of handbooks, papers or notes
has no curated layer, and `lw serve` does not serve one, deliberately — the wiki is the
access layer over context. But a person at a terminal should still be able to grep it,
and this is how.

**It keeps the base install honest.** If the only working connector required
`latent-wiki`, the claim that the client does not depend on the stack it fronts would be
untested.

Search is substring matching over a lead paragraph, not an index. That is the right size
for a few hundred files and obviously the wrong size for a million — at which point the
answer is a vector connector, not a better scan here.
"""

from __future__ import annotations

import re
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

#: Read at most this much of a file to find its lead. A lead lives in the first few
#: hundred bytes; reading whole files to rank them would make search cost the corpus.
_HEAD_BYTES = 4096

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n?", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


class FilesConnector:
    """Markdown files under one root, addressed by path relative to it."""

    kind = "files"

    def __init__(self, source_id: str, target: str, **options: Any) -> None:
        self.id = source_id
        raw = str(target)
        # `UPath` implements everything used here — `/`, `glob`, `open`, `is_dir` — but
        # is not a `Path` subclass to a type checker, so the narrowing is explicit.
        self.root: Path = (
            cast(Path, UPath(raw)) if "://" in raw else Path(raw).expanduser().resolve()
        )
        if not self.root.is_dir():
            raise ConnectError(f"no directory at {self.root}")
        self.pattern = str(options.get("pattern") or "**/*.md")
        self._files: list[Path] | None = None

    # -- layout ---------------------------------------------------------------

    @property
    def files(self) -> list[Path]:
        """Listed once. A directory scan over a network mount is not free."""
        if self._files is None:
            self._files = sorted(self.root.glob(self.pattern))
        return self._files

    def _key(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def _path(self, key: str) -> Path:
        """Resolve a key, refusing anything that climbs out of the root.

        A key arrives from a user, a config file, or an agent. `../../.ssh/id_rsa` is a
        valid relative path and must not be a valid key.
        """
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root.resolve())):
            raise ConnectError(f"{key!r} resolves outside {self.id}")
        return candidate

    # -- the contract ---------------------------------------------------------

    def describe(self) -> Descriptor:
        return Descriptor(
            id=self.id,
            kind=self.kind,
            title=self.root.name,
            capabilities=[Capability.SEARCH, Capability.FETCH, Capability.TOOLS],
            count=len(self.files),
            unit="files",
            detail={"root": str(self.root), "pattern": self.pattern},
        )

    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        needle = query.lower().strip()
        if not needle:
            return []
        hits: list[Hit] = []
        for path in self.files:
            key = self._key(path)
            head = _read_head(path)
            haystack = f"{key} {head}".lower()
            count = haystack.count(needle)
            if not count:
                continue
            # Weight the name over the prose: a match in a filename says more about what
            # a file *is* than the same match buried in a paragraph.
            score = float(count + 5 * (needle in key.lower()))
            hits.append(
                Hit(
                    ref=f"{self.id}:{key}",
                    source_id=self.id,
                    title=_title(head, key),
                    kind="file",
                    lead=_lead(head),
                    score=score,
                    provenance=Provenance(
                        source_id=self.id,
                        method="search",
                        ref=f"{self.id}:{key}",
                        origin=str(path),
                    ),
                )
            )
        hits.sort(key=lambda h: (-h.score, h.ref))
        return hits[:limit]

    # -- the agent's view -----------------------------------------------------
    #
    # A directory is as worth reading as a wiki, and the agent used to be unable to see
    # one at all: `files` offered no tools, so `intel doctor` reported it as "not
    # visible to the agent" and `ask` answered without it. Two tools is the whole fix —
    # they delegate to the same `search` and `fetch` a person uses.

    def tools(self) -> list[ToolSpec]:
        """Read-only. This connector never writes, so neither may the agent."""
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
        if name == "files_search":
            hits = await self.search(
                str(arguments.get("query") or ""),
                limit=int(arguments.get("limit") or 10),
            )
            if not hits:
                return f"no matches for {arguments.get('query')!r}"
            return "\n\n".join(f"{h.ref} — {h.title}\n  {h.lead}" for h in hits)
        if name == "files_get":
            doc = await self.fetch(str(arguments.get("key") or ""))
            return f"{doc.title}\n\n{doc.body}"
        raise ConnectError(f"{self.id}: no tool '{name}'")

    def server_spec(self) -> dict[str, Any] | None:
        """Served by our own module, so a directory needs nothing installed to be
        reachable by an agent — only its path, and the pattern if it is not markdown."""
        from .. import serve

        return serve.launch_spec(
            "files", str(self.root), self.id, {"pattern": self.pattern}
        )

    async def fetch(self, key: str) -> Doc:
        path = self._path(key)
        if not path.is_file():
            raise ConnectError(f"no file '{key}' in {self.id}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return Doc(
            ref=f"{self.id}:{key}",
            source_id=self.id,
            title=_title(text, key),
            body=text,
            kind="file",
            metadata={"path": str(path), "bytes": len(text)},
            provenance=Provenance(
                source_id=self.id,
                method="fetch",
                ref=f"{self.id}:{key}",
                origin=str(path),
            ),
        )

    async def aclose(self) -> None:
        self._files = None


def _read_head(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(_HEAD_BYTES)
    except OSError:
        return ""


def _body(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def _title(text: str, fallback: str) -> str:
    if match := _HEADING.search(_body(text)):
        return match.group(1).strip()
    return Path(fallback).stem.replace("-", " ").replace("_", " ")


def _lead(text: str) -> str:
    """The first real paragraph — the gist rung, same idea as a wiki page's lead."""
    body = _body(text).lstrip("\n")
    for block in body.split("\n\n"):
        stripped = " ".join(block.split())
        if stripped and not stripped.startswith("#"):
            return stripped[:280]
    return ""


_TOOLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "files_search",
        "Search file names and lead paragraphs under this directory. Returns refs and "
        "leads, not bodies — use files_get for one file in full.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    (
        "files_get",
        "One file in full, addressed by its path relative to the source root.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    ),
)
