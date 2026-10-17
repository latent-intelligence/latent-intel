# CLAUDE.md — latent-intel

## Project

`latent-intel` is a client over heterogeneous context: an LLM-wiki (`latent-wiki`), a
vector index (`latent-records`), a directory of markdown, a codebase, and any MCP server,
reachable together with a deployment's persona, skills and commands. It is a client of
every store and the server of none: a store is reached through its own MCP adapter
(`latent-wiki serve`, `latent-records serve`) as a `kind: mcp` source, like any other server.

Three frontends are planned. Two exist: a scriptable Typer/Rich CLI and a keyboard-first
`prompt_toolkit` shell. A web workbench comes later, and the whole design exists so that
it is an addition rather than a third implementation of the product.

How to use it: [`docs/getting-started.md`](docs/getting-started.md), then
[`docs/user-guide.md`](docs/user-guide.md).
Design record: `docs/context/` in this package.

## Package invariants

Four. Each is cheap to hold now and expensive to retrofit.

1. **Events are transport-grade.** Every event is a Pydantic model with a common envelope
   (`schema_version`, `event_id`, `session_id`, `operation_id`, `parent_id`, `sequence`,
   `ts`) and JSON-serializable fields. Not "usually serializable" — one field that only
   works in-process is discovered when the web client is written, which is too late.
2. **A frontend imports `session`, `commands`, `events`, `models`, `ui` — and nothing
   else.** Enforced by `lint-imports` in `just check`, because the violation that breaks
   it passes every other test in the suite. Reaching a connector *through* `Session` is
   the design; importing one directly is not. When a frontend needs to show something
   about connectors or the registry, the answer is a method on `Session`
   (`available()`, `connector_kinds()`), never an import.
3. **Unknown events render; they never raise.** `parse_event` returns `UnknownEvent` for
   an unrecognised `type`. A newer producer must never break an older renderer, or every
   vocabulary addition becomes a breaking change for every frontend at once.
4. **Tool effects are declared, never inferred.** A tool that declares nothing is treated
   as `external_write` and reported by `intel doctor` as under-declared. Guessing from a
   name is a guess standing between an agent and a filesystem.

## Scope discipline (important)

**This package owns composition, not content.** It does not parse markdown into pages,
does not embed anything, does not decide what a wiki page means. Those belong to the
packages it fronts. If a connector needs domain logic, the logic belongs upstream and
the connector calls it.

**A client depends on a store's adapter, never on its code or its format.** A store owns
its capability (workspace ADR-001): latent-wiki's ranking, evidence and views live in
`latent-wiki serve`, latent-records' embedding and exact match in `latent-records serve`. This
package imports neither and reads neither format; it connects to the server. Without a
server, a wiki's pages are Markdown, and the generic `files` connector can read them
if pointed at `pages/`, with none of the wiki's tools.

**Search never merges rankings across sources.** A lexical count and a vector cosine are
different quantities. `find` returns `{source_id: [Hit]}`, `run(Find(...))` emits one
`RetrievalResult` per source, and the renderer prints a heading per source. If a future
change wants a single ordered list, it needs a normalisation argument first — and there
isn't one.

**Connectors return values, not events.** A connector author writes ordinary async
methods and never touches an envelope or a sequence number. This is what keeps the event
vocabulary free to change without touching a single connector.

## Conventions (package-specific)

- **Two extension points for capability; one for composition.** *Connectors* say where
  context comes from, and which tools an agent can call; *runtimes* say which agent loop runs.
  Both are Python entry points (`latent_intel.connectors`, `latent_intel.runtimes`), and our own adapters register
  through them so the extension path is the one we take ourselves.

  A **project** composes what already exists — sources, saved searches, prompt
  templates, branding — and is declarative YAML and markdown, because composition needs
  no code and must be authorable by whoever owns the deployment rather than whoever owns
  the engine. This amends the earlier "entry points, not a plugin directory" rule, which
  did not anticipate the distinction.

  The test is mechanical: **if it can be expressed as `list[Command]` over installed
  capabilities it is a composition and belongs in a project; if it needs a new
  `Connector` or `Runtime` it is a capability and belongs in an entry point.**
- **A project is read, never written.** The engine writes only the user's own config.
  `connect` records into that user's overlay for the active project; `disconnect` on a
  project-declared source records a detachment. A tool that edited the project file
  would make a checked-out deployment repo dirty on first use.
- **Relative paths in a project resolve against the project file, not the cwd.** Same
  discipline as `SourceSpec` recording a name rather than a resolved path, and the same
  failure if broken: something that only works on the machine that authored it.
- **The engine knows about no client.** No client name, path, bucket or store id in
  `src/` — `tests/test_projects_acceptance.py` greps for it. A registry default that
  pointed at one operator's home already leaked that operator's catalogue into every
  install; it is now per-install.
- **Capabilities compose.** `Connector` is minimal; `Searchable` / `Fetchable` /
  `ToolProvider` are separate Protocols declared in `describe()` and checked against the
  object at connect time. A source that claims a capability it does not implement fails
  when it is attached, not when someone searches and gets nothing.
- **Refs are `source:key`, and the label is ours.** A server prints its keys as
  `<server name>:<key>`; this client swaps that name for its own source id when it records
  a ref. No server prints the label, so no server needs to know what it was attached as
  (workspace ADR-003).
- **Failures inside `run()` are events.** A frontend iterating a stream over a transport
  has nowhere to catch an exception. Typed methods raise; `run` emits `AgentFailed`.
- **Colours are semantic tokens** from `ui/theme.py` — `[accent]`, `[dim]`, `[source]`,
  never `[green]`. Retheming is one file, and the web client needs the same token names
  in CSS.
- **Credentials never touch the config file.** Runtimes read the environment.
  `LATENT_INTEL_HOME` overrides both config and data directories, for tests and for
  multi-tenant hosts.
- **Sources are recorded as named, never as resolved.** A registered store keeps its
  registry id under `from:` and re-resolves wherever the config is read; a literal path
  is stored verbatim with `~` and `${VAR}` intact. Storing the resolved path made configs
  machine-specific *and* leaked the email address embedded in a Drive mount path.
- Escape user-supplied text with `rich.markup.escape` before printing it — a page title
  containing `[bold]` must not restyle the terminal.
- Tests mirror `src/`. A test against a real store server runs only where that store's
  package is installed, and skips otherwise; the rest of the suite needs no store:
  `tests/fixtures/streams/*.jsonl` exist so renderers are testable with no store, no
  network and no model.
- **A paid endpoint is called only when someone asks for that run.** Any request
  spending a credential — a model turn, a provider's catalogue behind a key, a probe to
  find out whether something works — happens because the person running the session
  asked for it, and not otherwise. Never from a test, never from a loop or a background
  task, never to satisfy curiosity mid-task. `tests/` reaches no network at all, which
  is why the stream fixtures exist. A captured example in `docs/` comes from a run
  someone asked for, and says so when it did not. Sweeping a provider's model list to
  see what answers is a request for permission rather than a default: a rate limit
  reached by diagnosing is one its owner then has to wait out, and the diagnosis
  becomes the outage. A credential is read only to report that it is set, by name —
  the same rule the whole `hosts` module follows, and it does not stop at `src/`.
- **`README.md` is hand-edited and confirmed before every change.** Re-read it
  immediately before touching it — it is rewritten between and during sessions, and a
  patch built against a copy read earlier silently reverts that work. Propose the change
  and wait for a yes; hold proposed edits rather than applying them mid-run, including
  under auto mode. This applies to `README.md` alone: everything under `docs/` is edited
  freely.

## Acceptance criteria

1. `just check` is clean: ruff, `mypy --strict`, `lint-imports`, pytest.
2. Every recorded event round-trips through JSON with its envelope intact.
3. A fabricated event `type` renders through both frontends rather than raising.
4. No module in `src/` reads a store's format (`manifest.json`, `wiki.yaml`,
   `context-store.yaml`), and the suite passes with no store package importable; a store
   is reached through its own server, and a test against one skips without it.
5. `intel search --json | parse_event` yields no `UnknownEvent` — the guarantee the web
   client will depend on, checked against live output rather than fixtures alone.
