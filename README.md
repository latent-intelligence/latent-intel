# latent-intel

A client over heterogeneous context. Connect an LLM-wiki, a vector index, a directory of
markdown and a third-party MCP server, and search across them from one place.

# Getting started 

To install the `latent-intel` client, run the following command:

```bash
uv tool install git+https://github.com/latent-intelligence/latent-intel
```

That puts `intel` on your PATH and works from any directory. Add `--reinstall` to update,
or `uv tool install .` from a clone if you are working on the engine itself.

```bash
intel doctor                     # what is installed, what is reachable, what is not
```

For `intel ask` — an agent answering over your sources — add the extras for the runtimes
you want. `api` brings the Anthropic and OpenAI SDKs that `custom` and `sdk-anthropic` run
on; `agents` adds the OpenAI Agents SDK for `openai-agents`:

```bash
uv tool install "latent-intel[api,agents] @ git+https://github.com/latent-intelligence/latent-intel"
```

## Attach context 

### Option A - attach a source directly 

Sources are attached one at a time and recorded in your own config. The can be local, remote, or both. 

```
intel connect s3://<bucket>/wikis/design --kind wiki --as design
  ✓ wiki    design    148 pages    [search fetch tools]

$ intel connect ~/knowledge/raw --kind files --as manuals
  ✓ files   manuals   170 files    [search fetch]


intel search "context collapse"
    [concept] context-collapse                                          0.94
      Asking a model to rewrite an accumulated context end to end…
    [source]  zhang2025-agentic-context-engineering                     0.71
      Argues that adapting a model through its context rather than…

intel                                    # the interactive shell
```

### Option B - use a project 

A **project** is one YAML file owned by the deployment rather than the engine, carrying
several sources at once plus branding and saved commands. This is the shape a real
engagement takes, and standing up the next one is another YAML file — no code change, no
release.

```yaml
# ~/deployments/research/research.yaml
name: research
branding:
  name: RESEARCH
  tagline: standing context, with provenance
sources:
  - id: design
    kind: wiki
    target: s3://<bucket>/wikis/design
  - id: notes
    kind: files
    target: ~/knowledge/raw
```

```bash
intel project add ~/deployments/research   # finds it in place; nothing is copied
intel project use research
intel                                      # branded shell, both sources attached
```

With a project active, `intel connect` still works — it records the source in *your*
overlay on top of the project, never in the project file. A shared project can be
committed to a client repo while each person adds their own scratch sources.

## Configure a deployment

A project file can grow into a **deployment directory** — one directory, committed to
whatever repo you keep it in. The engine reads it and never writes to it, so a checked-out
deployment repo stays clean on first use.

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

**Saved commands** are one markdown file each — a frontmatter block naming a `search`,
`prompt` or `sequence`, and a body. They compose what the engine already does, which is
why they need no code and no release. `skills/` and `persona.md` are marked PLANNED
deliberately: the keys parse today but nothing consumes them yet, and a directory that
looks live and does nothing is worse than one that says so.

**Add custom branding .** The wordmark, tagline, prompt and palette all come from
the project:

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

The palette is semantic — you set roles, not a stylesheet, so a colour changes everywhere
it means the same thing and nowhere it does not. On a narrow terminal the brand degrades
to its wordmark rather than wrapping.

Full detail: [configuring-a-deployment.md](docs/configuring-a-deployment.md).

## Configure compute

Sources say where context comes from; a **runtime** says where answers come from. Four
ship: `claude-cli`, which shells to the `claude` binary; `custom`, which runs our own loop
against a **host** — an endpoint declared as a row, and the row says which protocol it
speaks; and `sdk-anthropic` and `openai-agents`, which hand the loop to the Anthropic
SDK's tool runner and the OpenAI Agents SDK over the same rows, while tools stay ours.
Seven hosts ship, including Azure AI Foundry on both its surfaces, classic Azure OpenAI,
OpenRouter and any OpenAI-compatible server on your own machine.

`intel hosts` says what each one would cost you to set up, before you have chosen:

```
$ intel hosts

custom ← configured
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY),
FOUNDRY_RESOURCE (or ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host
'foundry-openai'
  ✓ openrouter         ← in use  OPENROUTER_API_KEY
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN,
AZURE_OPENAI_ENDPOINT, OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
```

Names of variables, never values — the whole block is safe to paste. Credentials come
from the environment or a `.env` beside the project, never from a config file. Choose
with `/runtime`, `/host` and `/model` in the shell, or `runtime:` in config;
`intel doctor` then diagnoses the one you chose.

Walked through end to end in
[demos/choosing-a-host.md](docs/demos/choosing-a-host.md).

## Documentation

| | |
|---|---|
| [getting-started.md](docs/getting-started.md) | install, credentials, a first source, a first project |
| [user-guide.md](docs/user-guide.md) | every command, `--json` scripting, MCP servers, choosing a runtime, what to do when something is wrong |
| [configuring-a-deployment.md](docs/configuring-a-deployment.md) | the deployment directory, branding, saved commands |
| [demos/](docs/demos/) | scripted walkthroughs with captured output |


Two extension points, and deliberately only two. **Connectors** say where context comes
from; **tools** say what an agent can do. Both are entry points, and our own adapters
register through the same ones a third party would use — see
[CLAUDE.md](CLAUDE.md) for where the boundary falls.

## Contributing

```bash
uv sync --extra dev              # the venv, with test and lint tooling
just check                       # ruff · mypy --strict · import contracts · pytest
uv tool install . --reinstall    # put your build on PATH
```

`just check` is what CI runs, and it passes on a fresh clone with nothing beside it — no
sibling package is needed to build, test or type-check. It also runs the import
contracts, so some architectural rules fail the build rather than review; they are stated
once, in [CLAUDE.md](CLAUDE.md).

If you are also *authoring* a wiki, add your wiki compiler alongside the engine:
`uv tool install . --with <wiki-compiler> --reinstall`. That is only for reading an
**unpublished** store — a working directory with no `manifest.json`.

## Status

Early, but usable. Both terminal frontends work over the `files`, `wiki` and `mcp`
connectors, and `intel ask` runs against every runtime above — the `api` extra installs
the two in-process SDKs, and `agents` adds the OpenAI Agents SDK.
