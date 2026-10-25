# CLI reference

Every command, what it is for, and the options that matter. `intel <command> --help` lists every
option. Bare `intel` opens the shell ([use the shell](guides/use-the-shell.md)). `latent-intel`
is the same program under a longer name.

| Command | Does |
|---|---|
| [`intel`](guides/use-the-shell.md) | Open the interactive shell |
| [`connect`](#connect-and-disconnect) | Attach a source and record it in your config |
| [`disconnect`](#connect-and-disconnect) | Detach a source you attached |
| [`stores`](#stores) | What is attached, and what the registry offers |
| [`search`](#search-and-get) | Search every attached source; results grouped by source |
| [`get`](#search-and-get) | One document in full |
| [`ask`](#ask) | A question for the agent |
| [`run`](#run) | One of the active deployment's saved commands |
| [`replay`](#replay) | Render a recording |
| [`hosts`](#hosts-and-doctor) | Every host each runtime can reach, and what each needs |
| [`doctor`](#hosts-and-doctor) | What is installed, configured, attached and missing |
| `version` | The version, and the event schema it writes |
| [`deployment list`, `use`, `show`, `new`, `add`, `validate`](#deployment) | Find, choose, write and check deployments |
| [`eval run`](#eval-run) | Every case through every variant, each recorded |
| [`managed sync`](#managed-sync) | Create the agent `claude-managed-agents` runs |

Rendered output goes to stdout. Warnings, such as a source that failed to attach, and every `✗`
line go to stderr, so `--json` output pipes clean.

## connect and disconnect

```bash
intel connect ~/notes --kind files --as notes
intel connect ~/src/agent-harness --kind code --as repo
intel connect "latent-wiki serve --deployment ~/wikis/research" --kind mcp --as wiki
intel connect https://example.com/mcp --kind mcp --as docs
intel connect --from ./.mcp.json                 # the one server in the file
intel connect records --from ./.mcp.json         # one of several, by name
intel connect notes-vault                        # a registry id: no --kind
intel disconnect notes
```

- **`--kind`** is `files`, `code` or `mcp`, and is required for a path or URI. A registry id
  carries its own.
- **`--as`** sets the source id, which labels every ref; without it, the last segment of the path,
  or the registry id.
- **`--from FILE`** reads one server out of a `.mcp.json` (`mcpServers`) or `.vscode/mcp.json`
  (`servers`): its `command` and `args`, `url`, `env` and `cwd`. The file is read once.
- **`--remote`** uses a registry entry's mirror instead of its local copy.

The source is recorded under the active deployment in your config, never in the deployment file.
`disconnect` removes one you attached; it does not detach a source the deployment declares. How
to: [connect sources](guides/connect-sources.md).

## stores

Lists the attached sources with their kind, size and capabilities, then the registry's other
entries, marking those that cannot be connected.

## search and get

```bash
intel search "context collapse"
intel search "context collapse" --source notes --limit 3
intel get notes:context-collapse.md
intel get repo:src/loop.py:40-80          # a code key may end in a line span
intel get context-collapse.md             # a bare key: the first attached source
```

- **Results are grouped by source and never merged into one ranking.** A source with no match is
  listed with `no matches`, so it is not mistaken for one that was not searched.
- **`--limit`** (10) is hits per source.
- **`--kind`** is passed to each source as a type filter. No connector filters by type today: an
  `mcp` source refuses it, and `files` and `code` ignore it.
- **`--json`, `--record`, `--purpose` and `--title`** are as for [`ask`](#ask).
- **A bare key with nothing attached** ends in a traceback today, not a `✗` line.

A source that fails during a search is reported and the command exits 1; the other sources'
hits are still shown.

## ask

```bash
intel ask "What do the notes say about context collapse?"
intel ask "What changed this month?" --web search
intel ask "…" --record first-look --purpose demo --title "first look"
```

- **`--web off|search|browse`** sets the web scope for this question; the configured domain list
  and `max_uses` stay in force. **`--any`** drops them, and needs `--web`.
- **`--json`** prints one event per line instead of rendering.
- **`--record FILE`** renders as usual and appends every event to `FILE`, after a header. A plain
  name, with no suffix or directory, is `~/.local/share/latent-intel/recordings/<name>.jsonl`.
- **`--purpose share|demo|eval`** (`share`) and **`--title`** describe the recording, and are
  refused without `--record`.

The answer comes from the configured runtime ([choose a runtime](guides/choose-a-runtime.md)).
With none, `ask` says so and exits 1. Each `intel ask` is a new conversation; the shell keeps one.

## run

```bash
intel run                     # list the active deployment's saved commands
intel run brief context collapse
```

The words after the name are the argument, `{{argument}}` in the command
([saved commands](deployments.md#saved-commands)). Takes `--json`, `--record`, `--purpose` and
`--title`. `run` works even when a command's name collides with a shell built-in.

## replay

```bash
intel replay first-look
intel replay ./run.jsonl --since 12
intel replay first-look --json | jq -c .type
```

Renders a recording, or a saved `--json` stream, as it looked live. A bare name is found as
`--record` stores it. `--since N` starts at line N of the file. `--json` prints the lines
unchanged. A line it cannot read renders as an unknown event; what is wrong with the file as a
whole goes to stderr. An unreadable file exits 1.

## hosts and doctor

Neither calls a model. `hosts` reads only declarations and the environment; `doctor` also
attaches your configured sources.

- **`hosts`** prints each runtime's hosts: `✓` with the variables it reads, or what it still
  needs. `← configured` marks the runtime that answers, `← in use` its host.
- **`doctor`** prints the connectors, each runtime with its reason if it cannot run, the web
  scope and which runtimes honour it, the `.env` files loaded, the attached sources, tools with
  no declared effect, and the config and data paths.

Both name variables, never their values. [Choose a runtime](guides/choose-a-runtime.md) reads
their output.

## deployment

| Command | Does |
|---|---|
| `deployment list` | Every deployment found, `✓` on the active one; a name found twice is warned about |
| `deployment use NAME` | Make it active; `none` for none |
| `deployment show` | The active deployment's file, root, sources and settings, each with the layer it came from |
| `deployment new NAME [DIRECTORY]` | Write `DIRECTORY/NAME/NAME.yaml` from the worked example. Refuses a folder that exists and a name already found |
| `deployment add PATH` | Search a directory, or a file's directory, for deployments. Copies nothing |
| `deployment validate [NAME]` | Load one, the active one by default, and list its problems; exit 1 if any |

Deployments are found as `*.yaml` in, in order: `LATENT_INTEL_PROJECTS`, the directories `add`
recorded, `~/.config/latent-intel/projects/`, and the built-in `latent`. The first of one name
wins. How to: [set up a deployment](guides/set-up-a-deployment.md).

## eval run

```bash
intel eval run cases.yaml variants.yaml --repeat 3 --dry-run
intel eval run cases.yaml variants.yaml --repeat 3
```

Runs every case through every variant `--repeat` times (1), each run recorded beneath the active
deployment's root. `--dry-run` prints the plan and spends nothing. Paid variants are confirmed
first; `--yes` skips the question, and without a terminal they are refused unless it is given.
Exits 1 if any run failed. How to: [run an eval batch](guides/run-an-eval-batch.md).

## managed sync

```bash
intel managed sync --dry-run      # print the agent definition; no request
intel managed sync
```

Creates or updates the agent for the active deployment and records its ids in your config.
Refused, except with `--dry-run`, when a source did not attach. How to: [use managed agents](guides/use-managed-agents.md).

## Exit codes

| Code | Means |
|---|---|
| `0` | Done. Also a question stopped at a ceiling with an answer so far |
| `1` | A `✗` line: the command failed or was refused. A stream that carried a failure event, such as no runtime, a refused web scope or a source that failed during a search. A `deployment validate` that found problems, an `eval run` with a failed run |
| `2` | A usage error: an unknown option, a missing argument, or a command group given no command |

## Environment variables

| Variable | Meaning |
|---|---|
| `LATENT_INTEL_HOME` | Config and data in one directory, data in its `data/` |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME` | Move `~/.config` and `~/.local/share` |
| `LATENT_INTEL_PROJECT` | The active deployment for this command, over `deployment use` |
| `LATENT_INTEL_PROJECTS` | More directories to find deployments in, separated by `:` (`;` on Windows) |
| `KNOWLEDGE_REGISTRY` | The registry file, when the deployment names none |
| `LATENT_INTEL_CUSTOM_HOST`, `LATENT_INTEL_ANTHROPIC_SDK_HOST`, `LATENT_INTEL_OPENAI_AGENTS_HOST`, `LATENT_INTEL_CLAUDE_AGENT_SDK_HOST` | A runtime's host, where no `host:` is set |
| Each host's credentials | [The hosts](runtimes.md#the-hosts) |
| `AWS_*` | Object storage credentials, read by `botocore` |

Any of them may be set in a `.env` ([credentials](deployments.md#credentials)). The ones that
locate the config or the deployment, `LATENT_INTEL_HOME`, `XDG_CONFIG_HOME`, `LATENT_INTEL_PROJECT`
and `LATENT_INTEL_PROJECTS`, also decide which `.env` files are read; set in a `.env`, they come too
late for that, so export them.
