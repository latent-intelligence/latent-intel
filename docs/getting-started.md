# Getting started

latent-intel searches and asks across your notes, code, wikis and MCP servers, with answers that
cite their sources. Everything on this page up to [Ask a question](#ask-a-question) runs offline
and costs nothing.

## Install

Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install "latent-intel[all] @ git+https://github.com/latent-intelligence/latent-intel"
```

This puts `intel` on your `PATH`. Add `--reinstall` to update. The base install, without
`[all]`, has everything but two runtimes:

| Extra | Adds |
|---|---|
| none | The `files`, `code` and `mcp` connectors, and four runtimes: `claude-cli`, `custom`, `anthropic-sdk`, `claude-managed-agents` |
| `openai-agents` | The OpenAI Agents SDK, for the `openai-agents` runtime |
| `claude-agent-sdk` | The Claude Agent SDK, which bundles the Claude Code binary (about 200 MB, no Windows wheel yet), for the `claude-agent-sdk` runtime |
| `all` | Both |

No other package is needed. A wiki or a records store is reached through its own MCP server,
installed with its own package. Working on latent-intel itself: [CONTRIBUTING](../CONTRIBUTING.md).

Check what you have:

```
$ intel doctor
latent-intel 0.1.0

connectors (entry points)
  ✓ code
  ✓ files
  ✓ mcp

runtimes
  custom loop
    · custom — no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
…
```

Captured on a machine with no keys set and nothing attached. The rest of the output is read in
[choose a runtime](guides/choose-a-runtime.md).

## Attach a folder of notes

Any directory of Markdown is a source. Here, `~/notes` holds three invented notes, one of them
`context-collapse.md`:

```
$ intel connect '~/notes' --kind files --as notes
✓ files  notes  3 files  search fetch tools
recorded in ~/.config/latent-intel/config.yaml
```

The source stays attached for later commands, and for the shell, until you
`intel disconnect notes`.

## Search and read

```
$ intel search "context collapse"
searching notes…

notes  2 hit(s)
  file notes:context-collapse.md  1
    Rewriting a long context end to end loses detail at every pass. Small, local edits keep what
earlier passes learned.
  file notes:incremental-updates.md  1
    Add or change one item at a time instead of regenerating the whole context. It is the usual
remedy for context collapse.

$ intel get notes:context-collapse.md
```

Each hit has a ref, `source:key`, and `get` prints that document in full. With several sources
attached, results come back under one heading per source, never merged into one ranking.

## Ask a question

`ask` needs a runtime. The quickest is `claude-cli`, which uses an installed, signed-in Claude
Code, so it needs no key. Choose it once in the shell, which remembers it:

```
$ intel
latent › /runtime claude-cli
```

Then ask something your notes answer:

```
$ intel ask "What do my notes say causes context collapse?"
```

The agent searches `notes`, reads what it finds, and answers citing refs such as
`notes:context-collapse.md`. The answer is not shown, because it was not captured from a run. A
question is paid for by whatever the runtime uses: your Claude Code plan here, or a provider's
key on the other runtimes ([choose a runtime](guides/choose-a-runtime.md)).

## What lands on disk

```
~/.config/latent-intel/
└── config.yaml            the active deployment, your runtime choice and attached sources
~/.local/share/latent-intel/
├── history                the shell's
└── recordings/            runs saved with --record NAME
```

```yaml
approval: ask
runtime: claude-cli
overlays:
  default:
    sources:
    - id: notes
      kind: files
      target: ~/notes
```

The path is recorded exactly as `intel` received it. Quoted, as above, it keeps its `~`, so the
file works on another machine; unquoted, your shell expands it first.
An `mcp` source's `env` is kept as given: a bare `NAME` forwards your value and stores only the
name, while a `NAME=value` literal, typed or imported with `--from`, is stored as written. Keep
secrets in the environment or a `.env`, never as literals. `LATENT_INTEL_HOME=$(mktemp -d)`
gives a throwaway setup to try things in. Every key: [your config](deployments.md#your-config).

## Next

- [The guides](index.md#guides): connect more sources, use the shell, set up a deployment.
- [CLI](cli.md) and [Python library](library.md).
