# Deployment declaration

Every key of a deployment file, and of your own `config.yaml`, with defaults. How to set one up
is in [set up a deployment](guides/set-up-a-deployment.md); persona, skills and subagents are in
[shape the agent](guides/shape-the-agent.md).

A deployment is a YAML file, `<name>.yaml`, and the directory around it. This package reads it
and never writes it: what you change from the CLI or the shell goes into your own config instead.

## Layout

```
research/                 anywhere on disk; `intel deployment add` copies nothing
├── research.yaml         the declaration
├── .env                  credentials, optional; keep it out of version control
├── persona.md            agent: persona:
├── skills/               name.md or name/SKILL.md
├── agents/               one .md per subagent
├── commands/             one .md per saved command
└── logo.txt              branding: logo:
```

Only `research.yaml` is required. `.env` has that fixed name beside it; the other names are
whatever the file points at.

## A complete example

```yaml
schema_version: 1
name: research
title: Research
description: Notes and papers on context engineering.

vars:
  NOTES: ~/notes                         # an exported NOTES wins over this

sources:
  - {id: notes, kind: files, target: "${NOTES}"}
  - {id: repo, kind: code, target: ../agent-harness, options: {exclude: ".git .venv tests"}}
  - {id: wiki, kind: mcp, target: "latent-wiki serve --deployment ./wiki", options: {cwd: .}}
  - {id: docs, kind: mcp, target: "https://example.com/mcp"}

branding:
  name: RESEARCH
  tagline: notes on context engineering
  logo: ./logo.txt
  prompt: "research › "
  theme: {accent: "#7aa2f7"}

agent:
  persona: ./persona.md
  persona_mode: append
  runtime: anthropic-sdk
  runtimes:
    anthropic-sdk: {host: anthropic, model: claude-sonnet-5, max_tool_rounds: 15}

skills: ./skills
agents: ./agents
commands: ./commands

defaults:
  web: {mode: search, allowed_domains: [arxiv.org]}

root: ./products
```

`intel deployment new` writes a file holding every key, commented, from the worked example at
[`src/latent_intel/data/projects/latent.yaml`](../src/latent_intel/data/projects/latent.yaml).

## Paths and variables

- **Relative paths resolve against the deployment file's directory,** never the working
  directory: `target:` of a `files` or `code` source, `root:`, `registry:`, `persona:`,
  `skills:`, `agents:`, `commands:`, `branding: logo:` and a source's `cwd` option.
- **`${NAME}`** is replaced from the environment first, then from `vars:`, in every key above
  but `logo:`, and in source options whose value is a string (not in a list such as `env:`). An undefined one is reported, and the source or key
  using it is skipped.
- **A URI such as `s3://…`** is kept as written.
- **An `mcp` target is a command line or a URL,** so it is substituted but not resolved; a
  relative executable is what the `cwd` option is for.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `schema_version` | `1` | Accepted; no other version exists, and the value is not checked today |
| `name` | the file's name | Shown by `intel deployment show`. `none` and `default` are reserved. A deployment is found, chosen and keyed in your config by its file's name |
| `title` | the name | Shown by `intel deployment show` |
| `description` | empty | For people |
| `registry` | none | A registry file that `from:` names resolve against, instead of `KNOWLEDGE_REGISTRY` and `~/.config/latent-intel/registry.yaml` |
| `vars` | none | Location variables for `${NAME}`. Never credentials |
| `sources` | none | [Sources](#sources) |
| `branding` | Latent's | [Branding](#branding) |
| `agent` | none | [Agent](#agent) |
| `skills` | none | A directory of skills ([shape the agent](guides/shape-the-agent.md#skills)) |
| `agents` | none | A directory of subagents ([shape the agent](guides/shape-the-agent.md#subagents)) |
| `commands` | none | A directory of [saved commands](#saved-commands) |
| `defaults` | none | [Defaults](#defaults) |
| `root` | the file's directory | Where the deployment's products go: `intel eval run` writes `<root>/runs/`. A local path or a URI. An empty or unreadable `root:` is reported, never replaced by the default |

An unknown top-level key is reported and ignored. A directory key that names no directory is
reported. `intel deployment validate` lists these problems.

## Sources

| Key | Meaning |
|---|---|
| `id` | Required. The label in every ref, `id:key`. `web` and `engine` are reserved |
| `kind` | `files`, `code` or `mcp` |
| `target` | A path or URI for `files` and `code`; a command line or URL for `mcp` |
| `from` | Instead of `target`: a registry id, resolved on each machine |
| `remote` | With `from`: use the registry entry's mirror rather than a local copy. Default `false` |
| `options` | Per kind, below |

| Kind | Options |
|---|---|
| `files` | `pattern`: which files, default `**/*.md` |
| `code` | `include` and `exclude`: globs separated by spaces or commas, each replacing its default. `graph` is refused: there is no symbol index yet |
| `mcp` | `env`: a list of `NAME` (forward your value; refused when unset) or `NAME=value` (a literal). `cwd`: the directory the server starts in. `server_stderr: true`: show the server's own stderr |

Any other option is refused by an `mcp` source when it attaches; a `files` source ignores it,
and a `code` source refuses only `graph`. A source that fails to attach is reported and skipped;
the others still attach.

## Branding

| Key | Default | Meaning |
|---|---|---|
| `name` | `LATENT` | The name, and the banner's text on a narrow terminal |
| `tagline` | empty | The line under the logo |
| `logo` | Latent's block logo | A file beside the deployment, or an inline block scalar. A missing file falls back to Latent's logo with no message |
| `prompt` | the name in lower case, then ` › ` | The shell's prompt |
| `theme` | the built-in palette | Colours by token, below. An unknown token is warned about on every command |

| Tokens | |
|---|---|
| roles | `accent`, `accent.strong`, `text`, `dim`, `border`, `border.accent` |
| severity | `ok`, `warn`, `fail` |
| domain | `source`, `kind`, `score`, `ref`, `tool`, `prompt` |

A value is anything Rich reads as a style, such as `"#7aa2f7"` or `"bold #a7c080"`.

## Agent

| Key | Default | Meaning |
|---|---|---|
| `persona` | none | A Markdown file sent on every turn. An unreadable file is reported and left out |
| `persona_mode` | `append` | `append` adds it after the built-in instruction to cite as `source:key` and say when the sources do not answer, which is sent when sources are attached; `replace` puts it in that instruction's place. Anything else is reported and read as `append` |
| `runtime` | none | The runtime that answers, unless you choose another |
| `runtimes` | none | Options per runtime, merged key by key under yours ([options](runtimes.md#options)) |

## Defaults

| Key | Default | Meaning |
|---|---|---|
| `web` | `off` | `off`, `search`, `browse`, or `{mode, allowed_domains, blocked_domains, max_uses}`. One domain list at most, plain hostnames only. A value that does not parse turns the web off and is reported |
| `approval` | `ask` | Read, but not applied today: your config's `approval`, `ask` unless you set it, always wins |

## Saved commands

One Markdown file per command in `commands:`, named by its file or a `name:` in its frontmatter.
Run one with `/name` in the shell or `intel run name`.

```markdown
---
kind: prompt
description: Summarise what the sources say about a topic
argument: topic
argument_required: true
---
Summarise what the sources say about {{argument}}, citing each one.
```

| Key | For | Meaning |
|---|---|---|
| `kind` | all | `search`, `prompt` or `sequence`. Required |
| `description` | all | Shown by `/help` and `intel run` |
| `argument`, `argument_required` | all | The argument's name, and whether to refuse running without one |
| `query`, `source`, `limit` | `search` | What to search for, one source or all, hits per source (10) |
| body | `prompt` | The question asked |
| `steps` | `sequence` | Other commands' names, run in order; one cannot see another's result |

`{{argument}}` and `{{project}}` (the deployment's name) are replaced in `query` and the body. A
file that does not parse is reported and skipped. In the shell, a skill with a command's name
wins, and that is reported.

## Credentials

Credentials live in the environment, never in a YAML file. Two `.env` files are read, if they
exist: the active deployment's, beside its YAML file, then `~/.config/latent-intel/.env`. The
deployment's wins where both set a variable, and a variable already exported wins over both.
A `.env` in the working directory is never read. `intel doctor` names the files it loaded.

Object storage goes through `s3fs` and `botocore`, so `aws configure`, `AWS_PROFILE`, SSO and
instance roles work as they already do.

## Your config

`~/.config/latent-intel/config.yaml`, written by `intel` and safe to edit by hand. Unknown keys
are kept, but comments are not: every write rewrites the file.

| Key | Default | Meaning |
|---|---|---|
| `deployment` | none | The active deployment, set by `intel deployment use` |
| `deployment_paths` | none | Directories searched for deployments, added by `intel deployment add` |
| `runtime` | none | Wins over the deployment's `agent: runtime:` |
| `runtimes` | none | Options per runtime, laid over the deployment's key by key |
| `approval` | `ask` | `ask`, `never` or `auto` ([approval](runtimes.md#approval)) |
| `web` | none | Wins over the deployment's `defaults: web:`. `/scope` never writes it |
| `theme` | none | Tokens laid over the deployment's |
| `overlays` | none | What you changed per deployment, keyed by its file's name, or `default` with none active |

Each overlay holds `sources:` (rows like a deployment's, written by `intel connect`),
`detached:` (deployment source ids to leave out; nothing writes it today) and `runtimes:`
(what `intel managed sync` created). A source is recorded as you named it: a registry id under
`from:`, or a path exactly as typed, `~` and `${VAR}` included. A top-level `sources:` from an
older version is not read; `intel deployment show` names it so you can move it.

## Where state lives

| Path | Holds |
|---|---|
| `~/.config/latent-intel/` | `config.yaml`, `.env`, `registry.yaml`, and `projects/`, which is also searched for deployments |
| `~/.local/share/latent-intel/` | `history` (the shell's) and `recordings/` |

`XDG_CONFIG_HOME` and `XDG_DATA_HOME` move them. `LATENT_INTEL_HOME` replaces both: config in
it, data in its `data/`. The other variables are in [CLI](cli.md#environment-variables).
