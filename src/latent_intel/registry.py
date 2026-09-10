"""Reading the federated store registry, so a source can be named rather than located.

A registry is a catalogue of what exists **on this machine** — each entry carrying a
`kind` (`wiki`, `context-store`, `markdown-vault`) and an `access` (`filesystem`,
`mcp:<server>`). Those two fields are what connector selection needs, so
`intel connect <name>` works without this program holding a copy of anything.

The indirection is the point: a source recorded as a *name* re-resolves per machine, so
a project file shipped to a deployment host works there too. Recording a resolved path
would tie it to the machine that authored it. Point at a registry with
`KNOWLEDGE_REGISTRY`, or let a project carry its own `registry:`.

There is deliberately **no default store**. Searching the wrong store is worse than
failing to start — the same rule `latent_wiki.serve.resolve_root` follows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import ConnectError

#: Where a registry lives when nothing says otherwise: beside the config, per install.
#:
#: This used to be one operator's personal path, which meant a client running the tool
#: saw *that operator's* catalogue in `intel stores`. A shipped engine must not know
#: where anyone in particular keeps their notes; point at one with `KNOWLEDGE_REGISTRY`,
#: or better, let a project carry its own `registry:`.
DEFAULT_REGISTRY = "registry.yaml"
ENV_VAR = "KNOWLEDGE_REGISTRY"

#: Registry `kind` -> our connector kind. A registry entry we cannot map is listed but
#: not connectable, which is more useful than hiding it.
KIND_MAP = {
    "wiki": "wiki",
    "context-store": "files",
    "markdown-vault": "files",
}

#: Where to look for material, in order of preference. A context store usually has
#: several named paths; `raw` is the one worth searching.
PATH_PREFERENCE = ("root", "raw", "context", "summaries", "notes")


class Entry:
    """One registry row, reduced to what connecting needs."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        self.id = str(data.get("id") or "")
        self.name = str(data.get("name") or self.id)
        self.registry_kind = str(data.get("kind") or "")
        self.domain = str(data.get("domain") or "")
        self.access = str(data.get("access") or "filesystem")
        self.paths = {str(k): str(v) for k, v in (data.get("paths") or {}).items()}

    @property
    def kind(self) -> str | None:
        """The connector kind, or None when nothing here can serve it."""
        if self.access.startswith("mcp:"):
            return "mcp"
        return KIND_MAP.get(self.registry_kind)

    @property
    def mirror(self) -> str | None:
        """The remote copy, when the entry declares one."""
        return expand(self.paths["mirror"]) if "mirror" in self.paths else None

    def target(self, *, remote: bool = False) -> str | None:
        """What to point the connector at.

        **Local when it actually holds the store, the mirror otherwise.** Registry paths
        are machine-specific — a Drive path that exists on a laptop does not exist on a
        build host — so preferring local unconditionally makes every entry unusable
        anywhere else. `remote=True` forces the mirror, which is the deployment case and
        also how you test that the mirror is current.
        """
        if self.access.startswith("mcp:"):
            return self.access.split(":", 1)[1]
        if remote:
            return self.mirror
        for name in PATH_PREFERENCE:
            if name in self.paths:
                candidate = expand(self.paths[name])
                if "://" in candidate or has_content(Path(candidate)):
                    return candidate
        # No local path holds anything — this machine does not have the store. A mirror
        # is a better answer than "not connectable".
        return self.mirror

    @property
    def connectable(self) -> bool:
        return bool(self.kind and self.target())


def has_content(path: Path) -> bool:
    """Whether a local path actually holds something.

    **Existence is not readiness.** A cloud-synced folder — OneDrive, Drive — leaves an
    empty placeholder directory on a machine that has never pulled the data, and a
    deployment that keeps no local copy by design has exactly that. Treating the
    placeholder as the store made an entry resolve to an empty local path and never
    reach the mirror it declares, which failed later and further away, as a store with
    no manifest.
    """
    try:
        return path.is_file() or (path.is_dir() and any(path.iterdir()))
    except OSError:
        return False


def expand(value: str) -> str:
    """`~` and `${VAR}`, but leave a URI alone."""
    if "://" in value:
        return value
    return str(Path(os.path.expandvars(value)).expanduser())


def default_registry() -> Path:
    """The per-install registry. Imported lazily to keep this module free of `config`
    at module scope, which would be a cycle."""
    from .config import home

    return home() / DEFAULT_REGISTRY


def registry_path(explicit: str | None = None) -> Path:
    if chosen := (explicit or os.environ.get(ENV_VAR)):
        return Path(chosen).expanduser()
    return default_registry()


#: Parsed registries, keyed by (path, mtime). `resolve` is called once per source, so a
#: six-source session parsed the same file seven times — plus an eighth from
#: `Session.available()`. The mtime is in the key rather than checked against a stored
#: one, so a hand-edit during a live shell session is picked up and a stale entry cannot
#: outlive it.
_CACHE: dict[tuple[str, int], dict[str, Entry]] = {}


def clear_cache() -> None:
    """Forget parsed registries. For tests, and for anything that rewrites one."""
    _CACHE.clear()


def load(explicit: str | None = None) -> dict[str, Entry]:
    """Every registered store, by id. A missing registry is empty, not an error —
    naming a source is a convenience, and pointing at a path directly always works."""
    path = registry_path(explicit)
    if not path.is_file():
        return {}
    key = (str(path), path.stat().st_mtime_ns)
    if (cached := _CACHE.get(key)) is not None:
        return cached
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConnectError(f"{path}: {exc}") from exc
    rows = data.get("stores") or []
    entries = [Entry(row) for row in rows if isinstance(row, dict) and row.get("id")]
    parsed = {entry.id: entry for entry in entries}
    _CACHE[key] = parsed
    return parsed


def resolve(
    name: str, explicit: str | None = None, *, remote: bool = False
) -> tuple[str, str]:
    """Map a registered id to `(kind, target)`.

    Raises with the list of what *is* registered — a typo'd id is the common case, and
    the useful answer is the neighbouring correct one.
    """
    entries = load(explicit)
    entry = entries.get(name)
    if entry is None:
        known = ", ".join(sorted(entries)) or "nothing registered"
        raise ConnectError(f"no store '{name}' in the registry (known: {known})")
    target = entry.target(remote=remote)
    if remote and not target:
        raise ConnectError(
            f"'{name}' declares no mirror — remove --remote, or add `paths.mirror` "
            f"to its registry entry"
        )
    if not entry.kind or not target:
        raise ConnectError(
            f"'{name}' is registered as kind '{entry.registry_kind}' with no path this "
            f"build can connect to — point at it directly instead"
        )
    return entry.kind, target
