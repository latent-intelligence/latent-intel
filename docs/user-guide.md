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

**Working on the package itself?** A **user build** is copied at install time; a **dev
build** points at `src/` and tracks the working tree, so an edit is live and a
half-finished branch is too.

```bash
uv tool install ".[api,agents]" --reinstall              # user build — a snapshot of the checkout
uv tool install --editable ".[api,agents]" --reinstall   # dev build  — tracks the working tree
```

Both want the extras: `[api]` is the Anthropic and OpenAI SDKs that `custom` and
`sdk-anthropic` run on, `[agents]` is the OpenAI Agents SDK for `openai-agents`. Without
one, that runtime reports itself as not installed, since `uv run` reads the project
environment and a tool install does not. Or skip PATH — `just install`, which is
`uv sync --extra dev --extra api --extra agents`, and prefix commands with `uv run`.
Everything below works either way.

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
| `/record [file]` `/record off` | tee every event to a file · stop and say how many · bare, what is being recorded |
| `/runtime [name]` `/model [name]` `/host [name]` | which backend answers `ask`, which model it uses, which host it talks to |
| `/hosts` | every host either runtime can reach, and what each one needs |
| `/clear` `/exit` | clear the screen · leave (Ctrl-D also leaves) |
| `search <query>` | every attached source, grouped |
| `open <source:key>` | one document in full |
| `ask <question>` | the agent — see [runtimes](#no-agent-runtime-configured) |

### Which one do you type?

**Anything that is not a `/`-command and not one of those verbs is a question for the
agent** — so a bare line *is* `ask`, and the word is only ever needed to force it. The
difference that matters is who does the looking:

| you type | who acts | costs |
|---|---|---|
| `search context collapse` | **you** — over the attached sources, grouped per source | no model |
| `open design:context-collapse` | **you** — one document, verbatim | no model |
| `what did we decide about compaction?` | **the agent** — it searches and fetches for you, then writes | a model turn |
| `ask what did we decide about compaction?` | identical to the line above | a model turn |

`search` hands you hits to read; a bare prompt hands you an answer with the searching done
on your behalf. Use `search` when you know roughly what you are looking for, and a question
when you would rather be told.

**The one case that needs `ask`.** A line beginning with `search`, `open`, `get` or `ask`
is read as that verb, so `search strategies for grounding` searches — it does not ask. A
near-miss is caught and suggested rather than silently rewritten:

```
latent › searching for a good name is hard
✗ did you mean `search for a good name is hard`?
  or to ask it as a question: ask searching for a good name is hard
```

Prefix `ask` when your question genuinely starts with one of those four words.

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

intel doctor                                   # what is installed, reachable, undeclared
intel hosts                                    # every model endpoint, and what each needs
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

### Recording a run

`--json` sends events to stdout *instead of* rendering, which is wrong for a person: you
lose the live view precisely when you most want to keep it. `--record` tees — the run
renders as usual, and every event is appended to the file as it happens:

```bash
intel search "disclosure" --record run.jsonl   # also on get, ask and run
intel replay run.jsonl                         # the run, as it looked
intel replay run.jsonl --since 12              # start at line 12 of the file
intel replay run.jsonl --json | jq -c .type    # or pipe it onward
```

A recorded file is the `--json` stream, appended — two runs recorded to one file replay
as one, with no seam — and nothing beside it: no manifest, no index, no directory
convention. So an interrupted run still replays up to the interruption, and a run
recorded by a newer build replays here too: a line this one cannot read renders as the
unknown-event line rather than refusing the file, and a newer `schema_version` is named
on stderr rather than read silently.

`--since` is a line of the file, 1-based, because that is the number your editor shows
in the gutter — `sequence` restarts within one file, so it cannot address a position in
it. A file that cannot be read exits 1, and so does a `--record` target that cannot be
opened, before anything runs.

In the shell, `/record <file>` starts and `/record off` stops, reporting how many events
it wrote; bare `/record` says which file is being written, or that none is. Session
state, and nothing about it is written to your config.

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

### Bringing a server's own registration across

A server registration has four keys, and all four are honoured. Import one you already
have rather than retyping it:

```bash
intel connect --from ./.mcp.json            # one server in the file
intel connect records --from ./.mcp.json    # name one of several
intel connect records --from ./.vscode/mcp.json
```

The file is read once. What is recorded is an ordinary source row — the command, and the
options below — so nothing re-reads another host's config later.

`command` and `args` become the command line. `env` is a list, and **names travel where
values do not**:

```yaml
sources:
  - id: records
    kind: mcp
    target: records-mcp
    options:
      env: [AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]
      cwd: ./server
```

A bare `NAME` forwards your current value, which a `.env` beside the project file can
set; `NAME=value` writes a literal, for what is not a secret — a region, a data path.
A bare name that is unset is refused, rather than starting a server that fails later for
a reason nobody can see. In a `.mcp.json`, `"NAME": "${NAME}"` means the same thing and
imports as a bare name.

Credentials a server finds for itself need nothing forwarded: a server reading `~/.aws`
works with no `env` at all, because `HOME` is inherited.

`cwd` is for a server that looks for its own `.env` in the working directory, and for
`python -m`. A relative `cwd` in a project file resolves against the project file, never
against where you happened to be standing.

**On Windows**, two things differ and neither is a bug you can fix from here: the
inherited variable is `USERPROFILE` rather than `HOME`, which is what the AWS libraries
read there; and the command line is split POSIX-style, which eats backslashes — so name
a bare script on `PATH` rather than a full path.

**A server that will not start shows its own stderr.** `Connection closed` is the
account of a pipe; the sentence the server printed on its way out is the one that says
which credential was missing.

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

A **runtime** is **who owns the agent loop** — who decides when the model is asked again
and who runs the tools it asked for. A **host** is who serves the endpoint — one row in
that runtime's table, differing in credentials and model names and in nothing else.
`intel doctor` groups the installed runtimes by loop owner, because that is the
difference that decides what a runtime can reach and what you can configure about it.

`ask` needs a backend. Four ship: one delegates the loop to a binary on your machine, one
runs the loop we wrote, and two hand the loop to a vendor's SDK while tools stay ours.

- **`claude-cli`** shells to the `claude` binary and borrows the auth you already have —
  no API key. In exchange it owns its own agent loop, so it reaches your sources only as
  MCP servers: `files` and `vector` are invisible to it.
- **`custom`** runs the turn in this process, so our tool router runs and **every**
  attached source's tools are visible to it. One loop over any row in the table below:
  the **row carries the protocol**, so the same runtime reaches an Anthropic Messages
  endpoint and an OpenAI-compatible one without you choosing between them. Seven hosts
  ship. It has **no default host** — seven rows across two protocols make any default
  right for at most one deployment, so an unset host is reported by name — and on a chat
  row **no default model**, because every host names its models differently. Its host
  override is `LATENT_INTEL_CUSTOM_HOST`. It needs credentials in the environment and
  the `api` extra — from a clone `uv tool install ".[api]"`, or
  `uv tool install "latent-intel[api] @ git+https://github.com/latent-intelligence/latent-intel"`.
- **`sdk-anthropic`** is `custom` on a Messages row with the loop run by the Anthropic
  SDK's tool runner instead of by us. Same protocol, the two Anthropic hosts, the same
  credentials and the same options, and tools are still routed through our router, so it
  sees every attached source too. What it adds is what the SDK maintains: prompt caching
  is on, and the round-trip bound is the runner's. Its host override is
  `LATENT_INTEL_SDK_ANTHROPIC_HOST`, separate from `custom`'s, so one machine can point
  the two at different endpoints while comparing them.
- **`openai-agents`** is `custom` on a chat row with the loop run by the OpenAI Agents
  SDK instead of by us. Same protocol, the five chat hosts, the same credentials and the
  same options — `tokens_param` included, because the SDK's own parameter is the older
  name — and tools still route through our router, so every attached source is visible
  to it. It needs the `agents` extra, which pulls the framework alongside the `api`
  extra's SDKs: `uv tool install ".[api,agents]"`. Its host override is
  `LATENT_INTEL_OPENAI_AGENTS_HOST`. Two differences from `custom` worth knowing: an
  answer cut short by the output limit completes rather than reporting why, and a tool
  name the model invented is answered by the SDK rather than by our router, so no tool
  event appears for it.

`anthropic` and `openai` were folded into `custom` on 2026-09-17 — they named a wire
protocol rather than a loop owner, and the row names it now: set `runtime: custom` and
the `host:` the old runtime's name implied.

```
latent › /runtime custom
runtime custom
! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
latent › /host foundry-anthropic
host foundry-anthropic for custom
latent › /model claude-sonnet-5
model claude-sonnet-5 for custom
```

All three persist immediately, the way `/connect` does. `intel doctor` lists what is
installed and marks which one is configured. Bare `/model` and `/host` print the value in
force — what the runtime actually holds, including one set through the environment. The
model and the host are remembered **per runtime**, because neither means anything without
the backend it belongs to, and `/model none` or `/host none` clears your override; a value
the project declares stays in force, and the command prints what is in force:

```yaml
runtime: custom
runtimes:
  claude-cli:
    model: sonnet
  custom:
    host: openrouter         # required — no default; `intel hosts` lists the seven
    model: anthropic/claude-sonnet-5  # required on a chat row; a Messages row has one
    max_tokens: 16384
    max_tool_rounds: 10      # how many model round-trips one question may take
    tokens_param: max_tokens # chat hosts only — refused by name on a Messages row
  sdk-anthropic:             # the same options, and the two Anthropic hosts
    host: foundry-anthropic
    model: claude-sonnet-5
  openai-agents:             # the same options, and the five chat hosts
    host: foundry-openai
    model: my-gpt-deployment # a deployment name, not a catalogue id
```

The same block may be written by a project, under `agent:` — which is how a deployment
ships its runtime and model as configuration rather than setup:

```yaml
agent:
  runtime: custom
  runtimes:
    custom: {host: foundry-anthropic, model: claude-sonnet-5}
    openai-agents: {host: foundry-openai, model: my-gpt-deployment}
```

A model name is never validated here — we cannot enumerate them and a hardcoded list goes
stale. An unknown name fails when you ask, with the runtime's own message.

**What the agent can see.** A runtime that owns its own tool loop reaches your sources as
MCP servers, so under `claude-cli` only `mcp` and `wiki` sources are visible to it —
`files` and `vector` run in this process and have no server to point at. `intel doctor`
marks the difference under `attached`. Under `custom`, `sdk-anthropic` and
`openai-agents` the distinction does not apply: the tool router is ours either way —
whoever drives the loop — so everything attached is a tool.

**Credentials for the in-process runtimes.** Names only, always in the environment,
never in a config file.
A project's `.env` is loaded before the runtime is built, so deployment credentials live
there.

One table, whichever runtime dials it. `custom` reaches every row; `sdk-anthropic` the
two `messages` rows and `openai-agents` the five `chat` ones, because a runtime built on
one SDK can dial no other.

| host | protocol | needs | optional | model is |
|---|---|---|---|---|
| `anthropic` | messages | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` | a published id, defaulting to `claude-sonnet-5` |
| `foundry-anthropic` | messages | `ANTHROPIC_FOUNDRY_API_KEY`, and one of `ANTHROPIC_FOUNDRY_RESOURCE` / `ANTHROPIC_FOUNDRY_BASE_URL` | — | a deployment name, defaulting to `claude-sonnet-5` |
| `openai` | chat | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | a published id |
| `foundry-openai` | chat | `FOUNDRY_API_KEY`, and one of `FOUNDRY_RESOURCE` / `FOUNDRY_BASE_URL` | — | a deployment name |
| `openrouter` | chat | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` | `<vendor>/<model>` |
| `azure-openai` | chat | `AZURE_OPENAI_API_KEY` **or** `AZURE_OPENAI_AD_TOKEN`, plus `AZURE_OPENAI_ENDPOINT` and `OPENAI_API_VERSION` | — | a deployment name |
| `local` | chat | `LOCAL_OPENAI_BASE_URL` | `LOCAL_OPENAI_API_KEY` | whatever the server serves |

**The protocol column is why there is one runtime.** It is a property of the endpoint,
not of the loop: the two `messages` rows speak the Anthropic Messages protocol and the
five `chat` rows an OpenAI-compatible one, and `custom` picks the adapter off the row. A
`chat` row names no default model, because every one of them names deployments
differently; `tokens_param` is a `chat` setting and is refused by name on a `messages`
row rather than silently dropped.

**OpenRouter** is one key for open and low-cost models from every vendor at once; ids are
`<vendor>/<model>`, e.g. `anthropic/claude-sonnet-5`, listed at openrouter.ai/models.

**`local`** is any OpenAI-compatible server on your own machine — Ollama, vLLM, LM Studio
— so development costs nothing. It reads its own two variables, `LOCAL_OPENAI_*`: a key
issued for the public API must not be forwarded to whatever is listening on the LAN, and
one pair shared between the rows would mean the two hosts could not both be configured in
one `.env`. The address is what nothing can guess, and a key is optional because most such
servers ignore one (a placeholder is sent, since the SDK refuses to build a client without
any credential at all).

**`azure-openai` is the classic surface**, and distinct from Foundry's OpenAI-compatible
one — that is `host: foundry-openai`. It needs the dated `OPENAI_API_VERSION` as well as a
credential and the endpoint; there is no default version, because one guessed here goes
stale and returns a 400 that names neither the variable nor the fix.

The credential is **either** an API key **or** an Entra ID token in
`AZURE_OPENAI_AD_TOKEN` — a resource with key authentication disabled is the common
enterprise posture, and it cannot issue the key the other variable asks for. Set both and
the token is used: exporting one is the deliberate act, and the SDK refuses a client given
both anyway. A 401 names them both, so neither way in is a variable you have to know about
in advance.

**One Foundry resource has one key.** A machine already reaching Foundry over the
Anthropic protocol needs nothing more: `ANTHROPIC_FOUNDRY_API_KEY` and
`ANTHROPIC_FOUNDRY_RESOURCE` stand in for the `FOUNDRY_*` pair wherever it is unset, and
the reason names both. `ANTHROPIC_FOUNDRY_BASE_URL` does not stand in — it points at the
other surface of the same resource.

A host comes from `/host <name>` in the shell or `host:` in config, then from
`LATENT_INTEL_CUSTOM_HOST` (or the SDK runtimes' own overrides) where neither says
anything. `custom` has nothing beneath that: an unset host is reported by name rather
than defaulted. `/host` writes the same `host:` key, so the two are one setting and the
environment is the machine-by-machine fallback beneath both. `intel doctor` names exactly
which variable is missing, for the host that is configured — here on a machine with
nothing set at all:

```
runtimes
  custom loop
    · custom — no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  sdk runner
    · openai-agents — set OPENAI_API_KEY for host 'openai'
    · sdk-anthropic — set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  delegated
    ✓ claude-cli
```

A kind left under `runtimes:` that nothing provides is named too — `· anthropic —
options set under runtimes:, but no such runtime is installed` — because options nothing
reads are worse than options that are missing: the file says a host and a model are set
while neither reaches anything.

**`intel hosts` asks the same question of every host instead**, which is the one a
machine has before it has chosen one — what each endpoint would cost to set up, not why
today failed:

```
custom
  ! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  · openrouter         set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
```

Setting one up, host by host, is
[`configuring-compute.md`](configuring-compute.md) — the practical path, where the tables
above are the reference.

Both read the same declarations, so a host reported ready by one is ready to the other.
A runtime-level reason — a missing SDK, an unset model — stops every host at once and is
printed once above the table rather than against a host. `claude-cli` declares no hosts
and says so. Walked through end to end in
[`demos/choosing-a-host.md`](demos/choosing-a-host.md).

**Foundry resolves deployment names, not dated model ids.** `claude-sonnet-5` works;
`claude-sonnet-5-20260101` returns a 404, and the failure says so.

**Approval.** No runtime has an interactive prompt — `claude -p` has none, and there
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
