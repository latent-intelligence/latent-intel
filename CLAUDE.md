# CLAUDE.md — latent-intel

## Project

`latent-intel` is the access layer over heterogeneous context: an LLM-wiki
(`latent-wiki`), a vector index (`latent-records`), a directory of markdown, and
third-party MCP servers, reachable together. It is a *client* — `lw serve` is the server
side and stays in `latent-wiki`.

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

**But a client may not depend on the producer to read a published artifact.** A wiki
store publishes a versioned format — `manifest.json` plus `pages/` — and
`connectors/wiki.py` reads *that*, over fsspec, on a base install with no `latent-wiki`
anywhere. Installing the engine, pointing a project at `s3://…` and having it work is
the acceptance criterion; an install-time dependency on the package that *built* the
store defeats it. The two measured ranking decisions (filter-before-rank, rank over the
lead) are therefore reproduced in the connector with their reasons attached, and
`manifest_version` is the guard: a store this build cannot read is refused loudly rather
than misread quietly. The `wiki` extra remains, for one case only — an authoring machine
reading a store that has never been published, which has no manifest to read.

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
  context comes from; *tools* say what an agent can do. Both are Python entry points
  (`latent_intel.connectors`, `latent_intel.runtimes`), and our own adapters register
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
- **Refs are `store:key`** — the same colon rule `latent-wiki` uses, so a reference means
  the same thing in both programs.
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
- Tests mirror `src/`. Anything that needs a real store belongs in a manual check, not in
  the suite: `tests/fixtures/streams/*.jsonl` exist so renderers are testable with no
  store, no network and no model.

## Acceptance criteria

1. `just check` is clean: ruff, `mypy --strict`, `lint-imports`, pytest.
2. Every recorded event round-trips through JSON with its envelope intact.
3. A fabricated event `type` renders through both frontends rather than raising.
4. The base install reads a published wiki, local or `s3://`, with `latent_wiki`
   unimportable — `tests/test_wiki.py` forces that rather than trusting the machine.
   Only an unpublished store needs the extra, and says so by naming both ways out.
5. `intel search --json | parse_event` yields no `UnknownEvent` — the guarantee the web
   client will depend on, checked against live output rather than fixtures alone.

## Not yet built (in order)

- **Vector connector** over `latent-records`. Late and read-only: that package has no
  tests yet.
- **Agent runtimes** — `claude_cli` (`claude -p`, no API key), `api` (via
  `latent-extraction`), `openrouter`. The `Runtime` Protocol and entry points ship; no
  backend does. Note the asymmetry: with `api` and `openrouter` our tool router drives
  the loop, while `claude_cli` owns its own and receives our connectors as an
  `--mcp-config`. Both emit the same events; the difference stays inside `agent/`.
- **HTTP transport** — `Command` in, `AgentEvent` out. Mechanical once it is wanted.
- **Web workbench** — the reason for all of the above.
