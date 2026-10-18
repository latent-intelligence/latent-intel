# Latent-Intel

**Search and ask across all your context, with answers that cite their sources.**

Connect as many notes, papers, codebases, wikis and MCP servers as you need, and send
questions across all of them to the model or agent runtime you choose. It is also a
workbench for context engineering: write an agent's persona, skills and subagents as
plain files, then run evals on real questions to compare one version against another.
Pre-release.

![A tour of the intel shell: attached sources, a search, a page opened, and the tools](https://dievw70zlyn9e.cloudfront.net/ea3bf76edd571e19/shell-tour-v1.gif)

## Installation 

To install the `latent-intel` client, run the following command:

```shell
# install with all runtimes, including: `anthropic-sdk`, `claude-agent-sdk`, and `openai-agents`.
uv tool install "latent-intel[all] @ git+https://github.com/latent-intelligence/latent-intel"

# install with base runtimes/sdk.
uv tool install git+https://github.com/latent-intelligence/latent-intel
```

This puts `intel` on your PATH and works from any directory. Add `--reinstall` to update,
or `uv tool install .` from a clone if you are working on the engine itself. Six runtimes
ship; for more information see [choosing a runtime](docs/user-guide.md#choosing-a-runtime).

## Tutorial 

You can work in an interactive shell (`intel`) or run single commands (`intel search`,
`intel ask`) from a script. Either way, context reaches it through connectors: `files` for
a folder of documents, `code` for a codebase, and `mcp` for any MCP server, local or
remote. Attach sources together in a deployment, a YAML file you keep, or one at a time
from the command line.

```
# what is installed and configured
intel doctor

# create a deployment, name its sources (see later section), register and use it
intel deployment new ...
intel deployment add ~/deployments/latent
intel deployment use latent

# launch the workbench shell
intel

        ██       █████  ████████ ███████ ███    ██ ████████
        ██      ██   ██    ██    ██      ████   ██    ██
        ██      ███████    ██    █████   ██ ██  ██    ██
        ██      ██   ██    ██    ██      ██  ██ ██    ██
        ███████ ██   ██    ██    ███████ ██   ████    ██
                      context, with provenance

 ╭──────────────────────────── v0.1.0 ────────────────────────────╮
 │ search  ·  fetch  ·  tools             2 sources  ·  170 items │
 ╰────────────────────────────────────────────────────────────────╯

                  /help  /connect  /sources  /exit

# choose where answers come from: once, or whenever you want to change it
latent › /runtime claude-agent-sdk
latent › /host foundry-anthropic
latent › /model claude-sonnet-5-5

# search every attached source; results come back per source
latent › search context collapse
```

### Add context and knowledge sources 

A source is anything you want answers drawn from: a codebase, a wiki an LLM keeps, a
folder of notes or papers, or any MCP server, on your machine or remote.

#### Use a deployment

A **deployment** is one YAML file owned by its author rather than the engine, carrying
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
  - id: design                  # a wiki, through its own server
    kind: mcp
    target: "latent-wiki serve --deployment s3://<bucket>/deployments/design"
  - id: notes
    kind: files
    target: ~/knowledge/raw
```

Any MCP server can be a source. Set `kind: mcp`, and make `target:` the command that
starts the server, or its URL. The `design` source above is one: `latent-wiki serve` is
the server of a companion package, as `latent-records serve` is for a records store.
Neither package is released yet.

#### Use command line 

Sources are attached one at a time and recorded in your own config. They can be local,
remote, or both.

```shell
intel connect "latent-wiki serve --deployment s3://<bucket>/deployments/design" --kind mcp --as design
  ✓ mcp    design   157 items  tools search fetch

intel connect ~/knowledge/raw --kind files --as manuals
  ✓ files  manuals  170 files  search fetch tools

intel search "context collapse"
  design  2 hit(s)
    concept design:context-collapse  12
      Asking a model to rewrite an accumulated context end to end…
    source design:zhang2025-agentic-context-engineering  6
      Argues that adapting a model through its context rather than…

  manuals  1 hit(s)
    file manuals:context-collapse.md  1
      Rewriting a long context end to end loses detail at every pass.
```

### Context engineering

A deployment can grow from one YAML file into a **deployment directory**, which also
shapes how the agent behaves. Three kinds of plain markdown file do that:

- **`persona.md`**: the agent's voice and standing instructions, on every turn.
- **`skills/`**: know-how the agent loads when a question needs it, or that you run by
  name, such as `/methodology`.
- **`agents/`**: subagents it can hand work to, under `claude-agent-sdk`.

They follow Claude Code's file conventions, so one folder serves this engine, Claude Code
and the Agent SDK. **Saved commands** in `commands/` are shortcuts such as `/brief`. Each
is one markdown file naming a search, a prompt or a sequence of steps, so adding one needs
no code.

```
~/deployments/research/          anywhere on disk; `deployment add` copies nothing
├── research.yaml                the declaration — sources, branding, defaults
├── logo.txt                     wordmark, or inline in the YAML
├── commands/                    one .md per saved command  →  /brief, /recent
│   ├── brief.md
│   └── recent.md
├── corpus/                      material this deployment ships, if any
│   └── notes/
├── .env                         optional credentials — git-ignored, never committed
├── skills/                      name.md or name/SKILL.md  →  loaded when a question needs one
│   └── methodology.md
├── agents/                      one .md per subagent  →  delegates, claude-agent-sdk only
│   └── reviewer.md
└── persona.md                   voice and standing instructions, on every turn
```

Full detail: [configuring-a-deployment.md](docs/configuring-a-deployment.md).

### Configure compute

Sources say where context comes from; a **runtime** says where answers come from. Six
ship, grouped by who runs the agent loop: our own loop (`custom`), a vendor's SDK in this
process (`anthropic-sdk`, `openai-agents`, `claude-agent-sdk`), a harness already on your
machine (`claude-cli`, which uses your Claude Code login, so it needs no key), or a
vendor's service (`claude-managed-agents`). The others reach Anthropic, Azure AI Foundry,
Azure OpenAI, OpenAI, OpenRouter or a model on your own machine, reading keys from the
environment or a `.env` file, never from a config file. `intel hosts` shows what each one
still needs, and [configuring-compute.md](docs/configuring-compute.md) walks through each.


### Customize the workbench

**Add custom branding.** The wordmark, tagline, prompt and colours all come from the
deployment:

```
$ intel deployment use research
$ intel

    ██████  ███████ ███████ ███████  █████  ██████   ██████ ██   ██
    ██   ██ ██      ██      ██      ██   ██ ██   ██ ██      ██   ██
    ██████  █████   ███████ █████   ███████ ██████  ██      ███████
    ██   ██ ██           ██ ██      ██   ██ ██   ██ ██      ██   ██
    ██   ██ ███████ ███████ ███████ ██   ██ ██   ██  ██████ ██   ██
                   standing context, with provenance

 ╭────────────────────────────── v0.1.0 ──────────────────────────────╮
 │ search  ·  fetch  ·  tools                 2 sources  ·  170 items │
 ╰────────────────────────────────────────────────────────────────────╯

                    /help  /connect  /sources  /exit

research › search context collapse
```

Colours are set by role, such as the accent or the border, so one change recolours
everything that plays that role.

Full detail: [configuring-a-deployment.md](docs/configuring-a-deployment.md).


## Documentation

Start with [getting started](docs/getting-started.md), then the
[user guide](docs/user-guide.md). Both link to the rest.

## Contributing

Setting up and running the checks: [CONTRIBUTING.md](CONTRIBUTING.md).

[MIT License](LICENSE)
