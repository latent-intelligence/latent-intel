# latent-intel

A client over heterogeneous context. Connect an LLM-wiki, a vector index, a directory of
markdown and a third-party MCP server, and search across them from one place.

## Getting started

Install once, then choose **one of two options**. They are alternatives, not steps — Option B
does not build on Option A, and most deployments only ever use Option B.

```bash
uv tool install git+https://github.com/latent-intelligence/latent-intel
```

That puts `intel` on your PATH and works from any directory. Add `--reinstall` to update,
or `uv tool install .` from a clone if you are working on the engine itself.

### Option A — attach a source directly

The quickest way to see it work. Sources are attached one at a time and recorded in your
own config, so nothing is shared and nothing needs authoring up front.

```bash
intel connect s3://<bucket>/li/latent-wiki/data/wikis/design --kind wiki --as design
intel search "context collapse"
intel                                    # the interactive shell
```

**No extras, no local copy.** A published wiki store is a versioned format —
`manifest.json` plus `pages/` — and the client reads that format directly from wherever
the store lives: a path, or `s3://` with your usual credentials. `latent-wiki` is what
you install to *build* a wiki, not to read one.

**A source can be named rather than located.** A registered store resolves to its local
copy when that path exists and to its `paths.mirror` otherwise, so one registry works on
a laptop and on a build host. `--remote` forces the mirror, which is also how you check
that a published store is current.

### Option B — use a project

A **project** is one YAML file owned by the deployment rather than the engine, carrying
several sources at once plus branding and saved commands. This is the shape a real
engagement takes.

```yaml
# ~/deployments/research/research.yaml
name: research
branding:
  name: RESEARCH
  tagline: standing context, with provenance

sources:
  - id: design
    kind: wiki
    target: s3://<bucket>/li/latent-wiki/data/wikis/design
    options:
      context: s3://<bucket>/li/knowledge/context   # the material the wiki summarises
  - id: notes
    kind: files
    target: ~/knowledge/raw
```

```bash
intel project add ~/deployments/research   # finds it in place; nothing is copied
intel project use research
intel                                      # branded shell, both sources attached
```

### They compose, and the project file stays read-only

With a project active, `intel connect` still works — it records the source in *your*
overlay on top of the project, never in the project file. So a shared project can be
committed to a client repo while each person adds their own scratch sources, and
`intel project show` reports which layer every value came from.

Standing up the next deployment is another YAML file — no code change, no release.

```
$ intel connect design-wiki --as design
  ✓ wiki    design    148 pages    [search fetch tools]
$ intel connect design-wiki --remote --as design   # the same store from S3, via its manifest
  ✓ wiki    design    148 pages    [search fetch tools]  built 2026-08-24
$ intel connect ~/knowledge/raw --kind files --as manuals
  ✓ files   manuals   170 files    [search fetch]
$ intel search "context collapse"

  design
    [concept] context-collapse                                          0.94
      Asking a model to rewrite an accumulated context end to end…
    [source]  zhang2025-agentic-context-engineering                      0.71
      Argues that adapting a model through its context rather than…

$ intel get design:context-collapse
$ intel                       # the interactive shell
```

Getting started: [`docs/getting-started.md`](docs/getting-started.md) · full walkthrough: [`docs/user-guide.md`](docs/user-guide.md).

## Customize

Everything a deployment owns lives in one directory, committed to whatever repo you keep
it in. The engine reads it and never writes to it.

```
~/deployments/research/          anywhere on disk; `project add` copies nothing
├── research.yaml                the project — sources, branding, defaults
├── logo.txt                     wordmark, or inline in the YAML
├── commands/                    one .md per saved command  →  /brief, /recent
│   ├── brief.md
│   └── recent.md
├── corpus/                      material this deployment ships, if any
│   └── notes/
├── .env                         optional credentials — git-ignored, never committed
├── skills/                      PLANNED — bodies offered to the agent
│   └── methodology.md
└── persona.md                   PLANNED — voice and standing instructions
```

**Every relative path resolves against `research.yaml`**, never your working directory, so
the same checkout works on any machine and in any cwd. Remote locations go through
`vars:`, which an environment variable of the same name overrides — a deployment points
at its own bucket without editing the committed file.

`skills/` and `persona.md` are marked PLANNED deliberately: the keys parse today but
nothing consumes them yet, and a directory that looks live and does nothing is worse than
one that says so.

### Credentials

**By default there is nothing to set.** Object storage goes through `fsspec` to
`botocore`, so `aws configure`, `AWS_PROFILE`, SSO and instance roles all work as they
already do on your machine. This package holds no credential code and stores no secret.

For a machine where none of that is set up — a fresh laptop, a container, a colleague
opening a deployment for the first time — drop a `.env` beside the project file:

```bash
# ~/deployments/research/.env
AWS_ACCESS_KEY_ID=…
AWS_SECRET_ACCESS_KEY=…
AWS_DEFAULT_REGION=us-east-1
```

`~/.config/latent-intel/.env` does the same thing machine-wide, and the project's file
wins where both name a variable.

Three properties worth knowing, because each is a decision rather than an accident:

- **An exported variable always beats the file.** `.env` fills gaps only, so
  `AWS_PROFILE=other intel search …` still works as a one-off. It is the same precedence
  a project's `vars:` follows.
- **Values never reach a command line.** A served source runs as a subprocess whose
  config is passed to the agent runtime as an argument — visible in `ps`. The child is
  handed the *path* and reads the file itself.
- **`.env` is git-ignored at every depth**, and a deployment directory is git-ignored
  too. Nothing here is designed to be committed.

A `.env` cannot select the active project; that is `intel project use`, which persists.

A tool installed at your organisation should say *your* name. The wordmark, tagline,
prompt and palette are all values a project supplies — no code, no plugin, no fork.

```yaml
# ~/deployments/research/research.yaml
branding:
  name: RESEARCH                      # wordmark, and the default prompt
  tagline: standing context, with provenance
  logo: ./logo.txt                    # or an inline block scalar
  prompt: "research › "               # defaults to the lowercased name
  theme:
    accent: "#7aa2f7"                 # semantic tokens only
    border.accent: "#7aa2f7"
```

```
$ intel project use research
$ intel

     ██████  ███████ ███████ ███████  █████  ██████   ██████ ██   ██
     ██   ██ ██      ██      ██      ██   ██ ██   ██ ██      ██   ██
     ██████  █████   ███████ █████   ███████ ██████  ██      ███████
     ██   ██ ██           ██ ██      ██   ██ ██   ██ ██      ██   ██
     ██   ██ ███████ ███████ ███████ ██   ██ ██   ██  ██████ ██   ██
                    standing context, with provenance

 ╭──────────────────────────── v0.1.0 ────────────────────────────╮
 │ search  ·  fetch                        2 sources  ·  412 items│
 ╰────────────────────────────────────────────────────────────────╯

                    /help  /connect  /sources  /exit

research › search context collapse
```

On a narrow terminal the same brand degrades to its wordmark rather than wrapping,
measured per brand — a logo wider or narrower than ours drops at its own threshold:

```
$ COLUMNS=40 intel

                RESEARCH
   standing context, with provenance
```

**The palette is semantic, not a stylesheet.** You set roles, so a colour changes
everywhere it means the same thing and nowhere it does not:

| | tokens |
|---|---|
| roles | `accent` · `accent.strong` · `text` · `dim` · `border` · `border.accent` |
| severity | `ok` · `warn` · `fail` |
| domain | `source` · `kind` · `score` · `ref` · `tool` · `prompt` |

An unrecognized token is **reported, not ignored** — a misspelled `acccent` tells you so
on stderr instead of silently rendering the default.

**Every field is optional.** A project that declares no `branding:` still runs, with the
Latent defaults. A logo narrower or wider than ours is measured per brand, so it degrades
to the plain wordmark on a narrow terminal rather than wrapping.

Saved commands are the same idea — one markdown file per command, in the directory the
project names:

```markdown
---
name: brief
kind: prompt
argument: topic
---
Summarize what the corpus says about {{argument}}, citing sources.
```

```
research › /brief nitrate levels
```

`kind` is `search`, `prompt` or `sequence`. They compose what the engine already does,
which is why they need no code and no release — see [Extending it](#extending-it) for the
two points where new *capability* plugs in.

**Our own branding takes the client path.** The engine's identity is a project file like
any other — `src/latent_intel/data/projects/latent.yaml` — so the path you use is the one
we use, and it cannot rot unnoticed. Copy that file to start: every field is shown in it,
commented, in the order the loader reads them.

```bash
intel project validate research   # what is wrong with it, without switching
intel project show                # every resolved value, and which layer set it
```

## Extending it

Two extension points, and deliberately only two.

**Connectors** say where context comes from. A minimal `Connector` plus composable
capabilities — `Searchable`, `Fetchable`, `ToolProvider` — declared in `describe()`, so
an MCP server offering only tools is not forced to fake a search method.

```toml
[project.entry-points."latent_intel.connectors"]
my-source = "my_package:MySourceConnector"
```

**Tools** say what an agent can do. The router unions connector tools, MCP server tools
and built-ins into one namespace, so a LanceDB search and a GitHub MCP call look the same
to the agent. Every tool declares its `effect` — `none`, `external_read`, `local_write`,
`external_write`, `destructive` — and approval gates on that declaration rather than
guessing from a name.

Our own adapters register through the same entry points a third party would use, so the
extension path is the one we take ourselves.

## Contributing

Installing to *use* it is in [Getting started](#getting-started); this is the working
copy.

```bash
uv sync --extra dev              # the venv, with test and lint tooling
just check                       # ruff · mypy --strict · import contracts · pytest
uv tool install . --reinstall    # put your build on PATH
```

`just check` is what CI runs, and it passes on a fresh clone with nothing beside it — no
sibling package is needed to build, test or type-check. If you are also *authoring* a
wiki, add `latent-wiki` by path, since it is not on an index:

```bash
uv tool install . --with ../latent-wiki --reinstall
```

That is only for reading an **unpublished** store — a working directory with no
`manifest.json`. Reading a published one never needs it.

`just check` also runs the import contracts, so some architectural rules fail the build
rather than review. They are stated once, in [`CLAUDE.md`](CLAUDE.md) — read the package
invariants there before a first change.

## Status

Early, but usable. Both terminal frontends work over the `files`, `wiki` and `mcp`
connectors, and `intel ask` runs against the `claude-cli` runtime. The vector connector,
the `api` and `openrouter` runtimes, the HTTP transport and the web frontend are declared
and unimplemented — `intel doctor` lists each as *declared, not implemented* rather than
failing when you reach for one.
