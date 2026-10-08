# Contract — the wiki tools

The tools an agent uses to read a `latent-wiki.store` v1 wiki and its `latent-wiki.context`
v1 store. One implementation, `connectors/wiki.py`, serves them both in process (`intel ask`,
the shell) and over MCP (`intel serve`), so the two cannot differ. This file is the contract
a client or a prompt can rely on. Every tool is read-only.

## The ladder

Disclosure is a ladder. The overview is the only routing step; every other tool goes
deeper. Each answer ends with a footer: `— <rung> · <source>:<subject> · store as of <date>`,
then `cut: <what was left out, and how to get it>` or `cut: nothing`.

| rung | tool | arguments (defaults) | returns | bound |
|---|---|---|---|---|
| route | `wiki_overview` | — | page counts by type and status, structures (`part_of` trees) with sizes, every page of a type with ≤ 12 pages | one screen |
| browse | `wiki_index` | `type`, `part_of`, `offset` (0), `limit` (50) | one line per page: type, key, title, flags, intent; children in step order | limit ≤ 200 |
| find | `wiki_search` | `query`, `type`, `tag`, `part_of`, `status`, `where` {field: value or [values]}, `include_retired` (false), `include_drafts` (true), `limit` (10) | gists with `matched on:` fields and draft/inferred flags | limit ≤ 50 |
| read | `wiki_get` | `key`, `section`, `offset` (0) | type, intent, parent, evidence count, section headings, then the body or one section | 8,000 chars per call |
| relate | `wiki_neighbors` | `key` | edges out and in, with derivation | 40 per direction |
| relate | `wiki_trace` | `key` | evidence, sources, authorship, lifecycle, parent chain, children | — |
| evidence | `wiki_evidence` | `key`, `entry`, `offset` (0) | a gist per cited context entry, or one entry in full | 20 entries; 12,000 chars |
| source | `wiki_raw` | `key` (page or entry), `offset` (0), `max_chars` (12,000) | source text via the entry's `raw_path` | 12,000 chars |

Projects may add `wiki_view` (`name`, `root`, `depth`) for declared views, and `load_skill`.

## Rules a client can rely on

- **Filters before ranking.** Retired pages (`superseded`, `deprecated`) are excluded by
  predicate. They are not down-weighted.
- **Ranking.** SQLite FTS5 over key, title, aliases, tags, intent and lead, with Porter
  stemming, weighted in that order. All terms must match first; if none do, any term will.
  An exact key, title or alias match is pinned first. Bodies are indexed only when the
  source sets `index_body`.
- **Paths never leave the store.** A `raw_path` or source must be relative and stay inside
  the context root. Entry keys are a single segment. Anything else is refused.
- **Omitted means default.** Every argument whose default is not empty declares it in the
  schema, so an MCP client that omits it gets the in-process behaviour.
- **Several sources.** When one server holds more than one tool-providing source, every
  tool is prefixed `<source>__`, and the server's instructions use the prefixed names.

## Versioning

A breaking change to a tool's name, arguments or meaning changes this document and the
connector together, and goes in the changelog. Adding an optional argument does not break
the contract.
