"""A codebase — the repo's own files, addressed by path and line span.

`files` already reads a directory, but a repository is not a directory of markdown: its
material is source in a dozen languages, its noise is `.git`, `.venv` and
`node_modules`, and the useful unit of reading is a span of lines rather than a whole
file. That is the whole difference, and it is enough to want a kind of its own.

**A key may carry a line span.** `src/app.py:40-80` is a path and the lines to read.
`Ref.parse` splits on the first colon only, so `repo:src/app.py:40-80` arrives here as
the key `src/app.py:40-80`, and the span is ours to read. A suffix that looks like a
span and is not a valid one is refused rather than read as part of a filename.

**Only listed files are readable.** The include and exclude globs decide what search
sees, and they decide what fetch will open too — otherwise `.env` or `.git/config` is
one guessed key away from an agent.

**There is no graph yet.** A named `graph:` is refused at connect rather than ignored: a
source that quietly searched text when asked to search symbols would look like one with
no symbols.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from fnmatch import fnmatch
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

DEFAULT_INCLUDE = (
    "*.py *.pyi *.js *.jsx *.mjs *.cjs *.ts *.tsx *.go *.rs *.java *.kt *.kts "
    "*.scala *.c *.h *.cc *.cpp *.hpp *.cs *.rb *.php *.swift *.lua *.r *.jl "
    "*.sh *.bash *.zsh *.sql *.proto *.graphql *.vue *.svelte *.css *.scss *.html "
    "*.md *.rst *.txt *.toml *.yaml *.yml *.json *.cfg *.ini *.mk "
    "Makefile justfile Dockerfile"
)
DEFAULT_EXCLUDE = ".git .venv node_modules dist build __pycache__"

#: Larger files are skipped by search: a symbol worth finding is not in a bundle or a
#: generated fixture, and scanning one would make every query cost it.
MAX_SEARCH_BYTES = 1_000_000

#: Enough paths for an agent to orient by, short of a listing that fills its context.
_LIST_LIMIT = 500

_SPAN = re.compile(r"(\d+)(?:-(\d+))?")
_SPAN_CHARS = frozenset("0123456789-")


class CodeConnector:
    """Files under one repository root, addressed by path relative to it."""

    kind = "code"

    def __init__(self, source_id: str, target: str, **options: Any) -> None:
        self.id = source_id
        raw = str(target)
        self.root: Path = (
            cast(Path, UPath(raw)) if "://" in raw else Path(raw).expanduser().resolve()
        )
        if not self.root.is_dir():
            raise ConnectError(f"no directory at {self.root}")
        if options.get("graph"):
            raise ConnectError(
                f"{self.id}: graph backends are not available yet, so "
                f"graph={options['graph']!r} cannot be honoured — remove it to read "
                f"the repository as text"
            )
        self.include = _globs(options.get("include"), DEFAULT_INCLUDE)
        self.exclude = _globs(options.get("exclude"), DEFAULT_EXCLUDE)
        self._files: list[Path] | None = None

    # -- layout ---------------------------------------------------------------

    @property
    def files(self) -> list[Path]:
        """Listed once, pruning excluded directories rather than filtering after."""
        if self._files is None:
            self._files = list(self._walk(self.root))
        return self._files

    def _walk(self, directory: Path) -> Iterator[Path]:
        for entry in sorted(directory.iterdir()):
            key = self._key(entry)
            if entry.is_symlink() or any(
                fnmatch(entry.name, p) or fnmatch(key, p) for p in self.exclude
            ):
                continue
            if entry.is_dir():
                yield from self._walk(entry)
            elif self._included(self._key(entry)):
                yield entry

    def _included(self, key: str) -> bool:
        parts = Path(key).parts
        prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
        if any(
            fnmatch(name, p) for name in (*parts, *prefixes) for p in self.exclude
        ):
            return False
        return any(fnmatch(parts[-1], p) or fnmatch(key, p) for p in self.include)

    def _key(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def _path(self, key: str) -> Path:
        """Resolve a key, refusing anything that climbs out of the root.

        Containment, not a string prefix: `/repo2/x` starts with `/repo` and is not
        inside it."""
        root = self.root.resolve()
        candidate = (root / key).resolve()
        if candidate != root and root not in candidate.parents:
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
            detail={"root": str(self.root), "graph": "no graph"},
        )

    async def search(self, query: str, *, limit: int = 10, **filters: Any) -> list[Hit]:
        needle = query.lower().strip()
        if not needle:
            return []
        hits: list[Hit] = []
        for path in self.files:
            key = self._key(path)
            text = _read_text(path)
            count = f"{key} {text}".lower().count(needle)
            if not count:
                continue
            score = float(count + 5 * (needle in key.lower()))
            hits.append(
                Hit(
                    ref=f"{self.id}:{key}",
                    source_id=self.id,
                    title=key,
                    kind="file",
                    lead=_lead(text, needle),
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

    async def fetch(self, key: str) -> Doc:
        path, sep, tail = key.rpartition(":")
        if sep and path and tail and set(tail) <= _SPAN_CHARS:
            match = _SPAN.fullmatch(tail)
            if not match:
                raise ConnectError(f"{tail!r} is not a line span — write 40 or 40-80")
            start = int(match.group(1))
            return self._read(path, start, int(match.group(2) or start))
        return self._read(key, None, None)

    def _read(self, key: str, start: int | None, end: int | None) -> Doc:
        path = self._path(key)
        if not path.is_file() or not self._included(self._key(path)):
            raise ConnectError(f"no file '{key}' in {self.id}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        first = 1 if start is None else start
        last = len(lines) if end is None else end
        if first < 1 or (start is not None and last < first):
            raise ConnectError(f"{first}-{last} is not a line span — write 40 or 40-80")
        if start is not None and first > len(lines):
            raise ConnectError(
                f"'{key}' has {len(lines)} lines; {first} is past the end"
            )
        last = min(last, len(lines))
        width = len(str(last))
        body = "\n".join(
            f"{n:>{width}}  {lines[n - 1]}" for n in range(first, last + 1)
        )
        ref_key = key if start is None else f"{key}:{first}-{last}"
        return Doc(
            ref=f"{self.id}:{ref_key}",
            source_id=self.id,
            title=ref_key,
            body=body,
            kind="file",
            metadata={"path": str(path), "lines": f"{first}-{last}", "of": len(lines)},
            provenance=Provenance(
                source_id=self.id,
                method="fetch",
                ref=f"{self.id}:{ref_key}",
                origin=str(path),
            ),
        )

    # -- the agent's view -----------------------------------------------------

    def tools(self) -> list[ToolSpec]:
        """Read-only, and declared so: an undeclared tool is held for approval."""
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
        if name == "search_code":
            hits = await self.search(
                str(arguments.get("query") or ""),
                limit=int(arguments.get("limit") or 10),
            )
            if not hits:
                return f"no matches for {arguments.get('query')!r}"
            return "\n\n".join(f"{h.ref} — {h.title}\n  {h.lead}" for h in hits)
        if name == "read_file":
            start = int(arguments.get("start") or 0) or None
            end = int(arguments.get("end") or 0) or None
            if end is not None and start is None:
                start = 1
            doc = self._read(str(arguments.get("path") or ""), start, end)
            return f"{doc.ref}\n\n{doc.body}"
        if name == "list_files":
            return self._list(
                str(arguments.get("glob") or ""), str(arguments.get("subdir") or "")
            )
        raise ConnectError(f"{self.id}: no tool '{name}'")

    def _list(self, glob: str, subdir: str) -> str:
        keys = [self._key(path) for path in self.files]
        if subdir.strip("/"):
            prefix = self._key(self._path(subdir.strip("/"))) + "/"
            keys = [k for k in keys if k.startswith(prefix)]
        if glob:
            keys = [k for k in keys if fnmatch(k, glob) or fnmatch(Path(k).name, glob)]
        if not keys:
            return "no files match"
        more = len(keys) - _LIST_LIMIT
        shown = "\n".join(keys[:_LIST_LIMIT])
        return f"{shown}\n… {more} more" if more > 0 else shown

    def server_spec(self) -> dict[str, Any] | None:
        from .. import serve

        return serve.launch_spec(
            "code",
            str(self.root),
            self.id,
            {"include": " ".join(self.include), "exclude": " ".join(self.exclude)},
        )

    async def aclose(self) -> None:
        self._files = None


def _globs(value: Any, default: str) -> list[str]:
    """Whitespace- or comma-separated when a string, so an option survives argv."""
    value = value or default
    if isinstance(value, str):
        return value.replace(",", " ").split()
    return [str(v) for v in value]


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_SEARCH_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _lead(text: str, needle: str) -> str:
    """The first line that matched, numbered — where to start reading."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line.lower():
            return f"{number}: {line.strip()}"[:280]
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            return f"{number}: {line.strip()}"[:280]
    return ""


_TOOLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "search_code",
        "Search the repository's paths and file contents. Returns refs and the first "
        "matching line of each file, numbered — use read_file to read around it.",
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
        "read_file",
        "One file from the repository with line numbers, addressed by its path "
        "relative to the root. Pass start and end to read only those lines.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["path"],
        },
    ),
    (
        "list_files",
        "Paths of the repository's files, optionally under one subdirectory or "
        "matching a glob such as '*.py' or 'src/*'.",
        {
            "type": "object",
            "properties": {
                "glob": {"type": "string"},
                "subdir": {"type": "string"},
            },
        },
    ),
)
