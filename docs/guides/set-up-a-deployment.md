# Set up a deployment

A deployment names a set of sources once, with the agent's shape, saved commands and branding,
so whoever uses it switches to it rather than attaching sources one by one. It is a YAML file and
the directory around it, kept wherever its owner keeps it, such as a repository of its own. Every
key: [deployment declaration](../deployments.md).

## Write one

```
$ intel deployment new research ~/deployments
✓ wrote ~/deployments/research/research.yaml
next: intel deployment add ~/deployments/research
then: intel deployment use research
```

The file holds every key, commented. `new` refuses a folder that exists and a name already in
use; a name is lowercase letters, digits and hyphens.

Fill in the sources. Relative paths resolve against the file, never your working directory, so
the folder works wherever it is checked out. An `mcp` target is a command, not a path, so
`cwd: .` starts it beside the file:

```yaml
name: research
title: Research

sources:
  - {id: notes, kind: files, target: ./notes}
  - {id: repo, kind: code, target: ~/src/agent-harness}
  - {id: wiki, kind: mcp, target: "latent-wiki serve --deployment ./wiki", options: {cwd: .}}
```

## Make it active

```bash
intel deployment add ~/deployments/research    # search this directory; nothing is copied
intel deployment use research
intel deployment show                           # sources, runtime, approval, web, and their layers
intel                                           # the shell, with its sources attached
```

`deployment use` saves the choice. For one command, or one terminal, `LATENT_INTEL_PROJECT=research`
does the same without saving anything; a command that attaches sources warns when it names no
deployment. `deployment use none`, or `LATENT_INTEL_PROJECT=none`, returns to no deployment.

`deployment add` also finds deployments one folder down, so adding `~/deployments` finds
`~/deployments/research/research.yaml` and each of its neighbours. It refuses a directory where it
finds none.

## Check it

```bash
intel deployment validate research
```

It lists every problem without switching: an unknown top-level key, a directory that is not there
(a source's included), an `mcp` command that is not on PATH, a skill that does not parse, a `${VAR}`
nobody defines. Each is reported and skipped, never fatal, so one bad source still lets the rest
attach; `validate` exits 1 while any remain. It starts no server, so a server that is installed
but fails to start is found by `intel doctor`, which shows which sources attached. A saved command
that does not parse is reported by the shell when it starts, and by `intel run`.

## Locations that change between machines

A path that differs per machine goes through `vars:`, and an environment variable of the same
name wins, so the committed file is never edited:

```yaml
vars:
  NOTES: ./notes          # export NOTES=s3://bucket/notes to read the remote copy
sources:
  - {id: notes, kind: files, target: "${NOTES}"}
root: ./products          # where products such as eval runs go; may be s3://…
```

`vars:` are for locations. Credentials go in a `.env` beside the deployment file, which only
the active deployment's commands read ([credentials](../deployments.md#credentials)).

## Saved commands

A saved command is one Markdown file in the deployment's `commands:` directory: a search, a
question, or a sequence of other commands, named by its file. This is `commands/recent.md`:

```markdown
---
kind: search
description: Recent notes on a topic
argument: topic
query: "{{argument}}"
source: notes
limit: 5
---
```

`/recent context collapse` in the shell, or `intel run recent context collapse`, runs it. The
keys: [saved commands](../deployments.md#saved-commands).

## Branding

```yaml
branding:
  name: RESEARCH
  tagline: notes on context engineering
  logo: ./logo.txt
  theme: {accent: "#7aa2f7", border.accent: "#7aa2f7"}
```

The shell then shows the logo and tagline in its banner, and `research › ` as its prompt.
Colours are set by role, so one token recolours everything playing that role
([tokens](../deployments.md#branding)).

## What it changes, and what it does not

Your own changes never touch the deployment file. `intel connect` adds a source to your overlay
for this deployment, and `/runtime` or `/model` set yours over its `agent:` values; `deployment
show` marks the layer of its sources, runtime, approval and web scope. The persona, skills and subagents come from the deployment only:
[shape the agent](shape-the-agent.md).

## Known limits

- **Without `logo:`, or when its file is missing,** the banner shows Latent's own logo, with no
  message. `name` appears in its place only on a narrow terminal. Give a logo.
- **`defaults: approval:` is not applied:** your config's value, `ask` by default, always wins.
- **`/deployment <name>` in the shell** keeps the sources already open until you restart it.
