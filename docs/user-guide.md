# latent-intel — user guide

Connect an LLM-wiki, a directory of markdown, and an MCP server, and work across them from
one place. Two ways in: a shell you enter, and commands you script.

Everything below was run against the DESIGN wiki — 148 pages — and the output is copied
from those runs.

- [Install](#install)
- [Attach a source](#attach-a-source)
- [The shell](#the-shell)
- [The commands](#the-commands)
- [Scripting with `--json`](#scripting-with---json)
- [MCP servers](#mcp-servers)
- [Where your state lives](#where-your-state-lives)
- [When something is wrong](#when-something-is-wrong)

---

## Install

Install it as a tool, so `intel` is on your `PATH` and works from any directory:

```bash
uv tool install git+https://github.com/latent-intelligence/latent-intel
uv tool install git+https://github.com/latent-intelligence/latent-intel --reinstall
```

Or `uv tool install .` from a clone, if you are working on the engine itself.

There is no extra to add and no sibling package to fetch first. That is the entire
install, and it is the same one a fresh clone gets.

**That is the whole install.** Files, MCP servers and wikis — local or `s3://` — all
work from it. A published wiki store is a versioned format (`manifest.json` plus
`pages/`), and the client reads that format directly, so reaching an existing wiki needs
nothing beyond this and credentials for wherever it lives.

### The one case this does not cover

An **unpublished** store — a working directory with no `manifest.json` — is the authoring
machine's case, and only the wiki compiler that writes it can read it. Add that package
alongside the engine rather than through an extra:

```bash
uv tool install . --with <wiki-compiler> --reinstall
```

Until you do, such a store is skipped with an explanation and the rest of your sources
still answer — one unreadable source never fails the whole command:

```
! design: /path/to/store has no manifest.json and `latent_wiki` is not installed.
  Publish the store with `lw manifest` (it is what `just publish` runs), or add the
  package by path:  uv tool install . --with ../latent-wiki --reinstall
```

`--kind vector` is declared but not implemented; `intel doctor` lists it as not installed.

**Working on the package itself?** `uv sync --extra dev` and prefix commands with
`uv run`. Everything below works either way.

Check what you got:

```
$ intel doctor
latent-intel 0.1.0

connectors (entry points)
  ✓ files
  ✓ mcp
  ✓ wiki
  · vector — not installed
```

---

## Attach a source

A source is anything you can search or read. Attach it once and it stays attached — the
CLI is a new process every time, so "attached" lives in a config file rather than in
memory.

**By registered name.** A registry records what exists on this machine and where, so a
name is enough. Point at one with `KNOWLEDGE_REGISTRY`, or let a project carry its own:

```
$ intel connect handbook-wiki --as handbook
✓ wiki   handbook  148 pages  search fetch tools
recorded in ~/.config/latent-intel/config.yaml
```

**By path or URI**, when it is not registered. The kind is required here — guessing it
from a directory's contents would be wrong exactly when it matters:

```bash
intel connect ~/knowledge/context/raw --kind files --as notes
intel connect s3://<bucket>/wikis/design --kind wiki --as design
```

A wiki and the material it summarises are a **pair**, and a project file can say so:

```yaml
sources:
  - id: design
    kind: wiki
    target: s3://<bucket>/wikis/design
    options:
      context: s3://<bucket>/li/knowledge/context     # optional
```

The pairing is what lets `wiki_raw` escalate from a page to the source it derives from.
Leave it out and search, `open` and every other tool still work — only that escalation
reports it has nothing to read.

**Local or remote.** A registered store resolves to its local copy when that path exists,
and to its `paths.mirror` otherwise — so one registry works on your laptop and on a build
host that has no Google Drive. `--remote` forces the mirror, which is also how you check a
published one is current:

```
$ intel connect design-wiki --remote --as design
✓ wiki   design  148 pages  search fetch tools    built 2026-08-24
```

That `built` date only appears on a store loaded through its `manifest.json`. If it is
missing on a remote store, the manifest was not published — publish it with your wiki
compiler and push again.

**What is attached, and what else is reachable:**

```
$ intel stores
attached
  handbook           wiki     148 pages      search fetch tools

available (intel connect <id>)
  manuals            files    Technical manuals — 11 volumes, 170 files
  papers             files    A reference corpus of published papers
  notes              files    Working notes, unstructured
```

Detach with `intel disconnect handbook`.

---

## The shell

Bare `intel` opens it. This is the primary way in.

```
              ██       █████  ████████ ███████ ███    ██ ████████
              ██      ██   ██    ██    ██      ████   ██    ██
              ██      ███████    ██    █████   ██ ██  ██    ██
              ██      ██   ██    ██    ██      ██  ██ ██    ██
              ███████ ██   ██    ██    ███████ ██   ████    ██

 ╭────────────────────────────────── v0.1.0 ──────────────────────────────────╮
 │ search  ·  fetch  ·  tools                         1 source  ·  148 items  │
 ╰────────────────────────────────────────────────────────────────────────────╯

                        /help  /connect  /sources  /exit

latent ›
```

**One rule of grammar: a leading `/` is session management, anything else is a query.**

```
latent › /sources
  ✓ wiki   design  148 pages  search fetch tools

latent › search progressive disclosure
searching design…

design  3 hit(s)
  source design:cochran2026-progressive-disclosure-wiki  4
    The system under study is uncannily close to mine: a personal knowledge base
    of 709 markdown pages — sources, entities, concepts, analyses, proposals…
  concept design:progressive-disclosure  4
    Give an agent identifiers and one-line descriptions first, and full content
    only once it has chosen…

latent › /use design
bare keys now resolve against design

latent › open progressive-disclosure

Progressive Disclosure
ref  design:progressive-disclosure
why  The access pattern this corpus most agrees on — and the one whose mechanism
     everyone guessed wrong.

Give an agent identifiers and one-line descriptions first, and full content only
once it has chosen…
```

| | |
|---|---|
| `/sources` | what is attached |
| `/connect <store>` | attach one — a registered id, or a path with `--kind` |
| `/disconnect <id>` | detach one |
| `/use [id]` | which source a bare key resolves against |
| `/tools` | what the router exposes, and each tool's declared effect |
| `/clear` `/exit` | clear the screen · leave (Ctrl-D also leaves) |
| `search <query>` | every attached source, grouped |
| `open <source:key>` | one document in full |
| `ask <question>` | the agent — see [runtimes](#no-agent-runtime-configured) |

Anything that is not a `/`-command and not one of those verbs is treated as a question.

**Editing.** History persists between sessions. Tab completes commands, source ids, and
the refs from your last search. Ctrl-C abandons the line you are typing; Ctrl-D leaves.
Output is ordinary scrollback — you can scroll back, select with the mouse, and pipe a
transcript somewhere.

**Attaching in the shell also records it**, so `intel search` in another terminal sees the
same sources.

---

## The commands

Everything the shell does is also a subcommand, for scripting and for one-offs.

```bash
intel search "progressive disclosure"          # every attached source
intel search "disclosure" --source design      # one source
intel search "disclosure" --kind concept       # by page type, where a source has types
intel search "disclosure" --limit 3            # hits per source
intel get design:progressive-disclosure        # one document
intel get progressive-disclosure               # bare key → the first attached source
```

**Results group by source and are never merged into one ranking.** A wiki's score is a
lexical count; a vector index's is a cosine. They are different quantities, and a single
ordered list would assert a comparison neither number supports. So each source gets its own
heading:

```
design  1 hit(s)
  concept design:progressive-disclosure  10
```

`--kind` is passed through to the source and ignored by sources that have no types. A
`files` source has none; a wiki has `concept`, `source`, `synthesis`, and whatever else its
pack declares.

---

## Scripting with `--json`

Any streaming command takes `--json` and emits one event per line, envelope included:

```
$ intel search "disclosure" --limit 1 --json | jq -c '{type, sequence, source_id}'
{"type":"retrieval_started","sequence":1,"source_id":null}
{"type":"retrieval_result","sequence":2,"source_id":"design"}
```

Every event carries `schema_version`, `event_id`, `session_id`, `operation_id`,
`parent_id`, `sequence` and `ts`. Results carry provenance:

```json
{
  "ref": "design:cochran2026-progressive-disclosure-wiki",
  "title": "…", "kind": "source", "lead": "…", "score": 4.0,
  "provenance": {
    "source_id": "design",
    "method": "search",
    "ref": "design:cochran2026-progressive-disclosure-wiki",
    "origin": "corpus:raw/cochran2026-progressive-disclosure-wiki.md",
    "as_of": "2026-07-06"
  }
}
```

`origin` is the underlying material the page derives from, and `as_of` is when that
material was true. A result that cannot say where it came from is worse than no result, so
provenance is a required field rather than a convention.

Errors go to stderr, so stdout stays machine-readable. A command that emits a failure
event exits 1.

---

## MCP servers

Any MCP server can be attached, ours or anyone's:

```bash
intel connect "npx -y @upstash/context7-mcp" --kind mcp --as ctx7
intel connect "https://example.com/mcp" --kind mcp --as remote
```

```
latent › /tools
  design.wiki_search           external_read
  design.wiki_get              external_read
  design.wiki_neighbors        external_read
  design.wiki_trace            external_read
  design.wiki_raw              external_read
```

Tool names are namespaced by source, so two servers can both offer `search` without one
shadowing the other. Each tool shows its **declared** effect — `none`, `external_read`,
`local_write`, `external_write`, `destructive`. A tool that declares nothing is treated as
`external_write` and listed by `intel doctor` as under-declared: absence is not permission.

Two things worth knowing:

**Point at the server, not at a wrapper.** Give it `uv run … <server>` and killing the
client kills `uv`, leaving the real server orphaned. Give it the executable —
`/path/.venv/bin/<server> …` — and cleanup is exact.

**Our own wiki does not need MCP.** `--kind wiki` reads the published store directly —
one GET for the manifest, page bodies only when asked: no subprocess, no tool-schema
round trip, and typed results instead of text. MCP is for servers you do not own.

---

## Where your state lives

```
~/.config/latent-intel/config.yaml     what is attached, and preferences
~/.local/share/latent-intel/           history and caches
LATENT_INTEL_HOME                      overrides both
```

The config is plain YAML and safe to edit:

```yaml
sources:
- id: design          # a registered store, re-resolved wherever this is read
  kind: wiki
  from: design-wiki
  remote: true        # prefer the mirror over any local copy

- id: notes           # a literal path, kept exactly as typed
  kind: files
  target: ~/knowledge/context/raw
approval: ask
```

**Sources are recorded as they were named, never as they resolved.** A registered store
keeps its registry id under `from:` and re-resolves on whatever machine reads the file, so
the same config works on a laptop with Drive mounted and on a host without it. A literal
path is stored verbatim, with `~` and `${VAR}` intact.

That matters for more than portability. An earlier version stored the resolved path, so
a config for `.../knowledge/wiki` recorded the full cloud-mount path it expanded to —
machine-specific, and on Drive that path contains your email address, in a file people
paste into chat threads.

Edit it by hand if you move a store, or `disconnect` and `connect` again.

**Credentials never go in this file.** Runtimes read them from the environment. A config
file gets copied into a gist and pasted into a support thread; an environment variable does
not.

`LATENT_INTEL_HOME` gives you a throwaway setup, which is the easiest way to try something
without disturbing your own:

```bash
LATENT_INTEL_HOME=/tmp/scratch intel connect manuals
LATENT_INTEL_HOME=/tmp/scratch intel search "icing"
```

---

## When something is wrong

**`intel doctor`** first. It reports installed connectors, runtimes, what is attached, and
any tool that did not declare an effect.

**"'x' is not connected"** — `intel stores` to see what is. Names come from `--as`, or from
the registry id.

**"no store 'x' in the registry"** — the message lists what *is* registered; a mistyped id
is the usual cause.

**A source fails to attach** — it is reported and skipped, not fatal. One unreachable Drive
path does not stop a search over the other four. The warning goes to stderr.

**A search returns nothing from one source** — that source is listed with `no matches`
rather than omitted, so you can tell "nothing there" from "not searched".

**`no page 'x' in design`** — the key does not exist. Search first; `open` takes the `ref`
a search prints.

### Choosing a runtime

A **runtime** is who runs the agent loop and which protocol it speaks. A **host** is who
serves the endpoint — one row in that runtime's table, differing in credentials and
model names and in nothing else.

`ask` needs a backend. Two ship, and they trade the same thing in opposite directions.

- **`claude-cli`** shells to the `claude` binary and borrows the auth you already have —
  no API key. In exchange it owns its own agent loop, so it reaches your sources only as
  MCP servers: `files` and `vector` are invisible to it.
- **`anthropic`** runs the turn in this process over the Anthropic Messages protocol, so
  our tool router runs and **every** attached source's tools are visible to it. It needs
  credentials in the environment and the `api` extra — from a clone
  `uv tool install ".[api]"`, or
  `uv tool install "latent-intel[api] @ git+https://github.com/latent-intelligence/latent-intel"`.

A runtime over the OpenAI protocol — OpenRouter, OpenAI, Azure OpenAI, local servers —
is the next one, and is not built yet.

```
latent › /runtime anthropic
runtime anthropic
latent › /model claude-sonnet-5
model claude-sonnet-5 for anthropic
```

Both persist immediately, the way `/connect` does. `intel doctor` lists what is installed
and marks which one is configured. The model is remembered **per runtime**, because a model
name means nothing without the backend it belongs to:

```yaml
runtime: anthropic
runtimes:
  claude-cli:
    model: sonnet
  anthropic:
    host: foundry            # or `anthropic`
    model: claude-sonnet-5
    max_tokens: 16384
    max_tool_rounds: 10      # how many model round-trips one question may take
```

The same block may be written by a project, under `agent:` — which is how a deployment
ships its runtime and model as configuration rather than setup:

```yaml
agent:
  runtime: anthropic
  runtimes:
    anthropic: {host: foundry, model: claude-sonnet-5}
```

A model name is never validated here — we cannot enumerate them and a hardcoded list goes
stale. An unknown name fails when you ask, with the runtime's own message.

**What the agent can see.** A runtime that owns its own tool loop reaches your sources as
MCP servers, so under `claude-cli` only `mcp` and `wiki` sources are visible to it —
`files` and `vector` run in this process and have no server to point at. `intel doctor`
marks the difference under `attached`. Under `anthropic` the distinction does not apply:
the loop is ours, so everything attached is a tool.

**Credentials for `anthropic`.** Names only, always in the environment, never in a config
file.
A project's `.env` is loaded before the runtime is built, so deployment credentials live
there:

| host | needs | optional |
|---|---|---|
| `foundry` (default) | `ANTHROPIC_FOUNDRY_API_KEY`, and one of `ANTHROPIC_FOUNDRY_RESOURCE` / `ANTHROPIC_FOUNDRY_BASE_URL` | — |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` |

`LATENT_INTEL_ANTHROPIC_HOST` selects the host when the config does not; an explicit
`host:` beats it. `intel doctor` names exactly which variable is missing:

```
runtimes
  ! anthropic — set ANTHROPIC_FOUNDRY_API_KEY for host 'foundry'
  ✓ claude-cli
```

**Foundry resolves deployment names, not dated model ids.** `claude-sonnet-5` works;
`claude-sonnet-5-20260101` returns a 404, and the failure says so.

**Approval.** Neither runtime has an interactive prompt — `claude -p` has none, and there
is no one to ask inside a stream — so the tool list is decided up front from each tool's
declared effect. Under `approval: ask` or `never` only non-writing tools are offered;
under `auto`, writes are too. `ask` therefore means the cautious end until this package
grows a real approval channel.

### No agent runtime configured

```
$ intel ask "which sources disagree about compaction?"
✗ no agent runtime configured
  choose one with `/runtime claude-cli` in the shell, or set `runtime:` in
  ~/.config/latent-intel/config.yaml. Run `intel doctor` to see which are available.
```
