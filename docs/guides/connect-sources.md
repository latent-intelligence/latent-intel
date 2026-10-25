# Connect sources

Attach a folder, a codebase or an MCP server, on your machine or remote, and detach it again.
This guide attaches one source at a time from the command line; to declare a set of them
together, [set up a deployment](set-up-a-deployment.md). Every option: [CLI](../cli.md#connect-and-disconnect).

## A folder of notes or papers

```bash
intel connect ~/notes --kind files --as notes
```

A `files` source is the Markdown under one directory. Search matches each file's path and its
first few kilobytes; `get notes:<path>` reads one file. The agent gets `files_search` and
`files_get`, both read-only. Another pattern than `**/*.md` is the `pattern` option, set in a
deployment ([sources](../deployments.md#sources)).

## A codebase

```bash
intel connect ~/src/agent-harness --kind code --as repo
intel get repo:src/loop.py:40-80
```

A `code` source is a repository's text files: source, config and docs, with `.git`, `.venv`,
`node_modules`, `dist`, `build` and `__pycache__` left out, and files over 1 MB skipped by search.
A key may end in a line span, `:40` or `:40-80`, and the lines come back numbered. A file left
out cannot be fetched either, so `.env` stays closed. The agent gets `search_code`, `read_file`
and `list_files`, all read-only.

## An MCP server

```bash
intel connect "npx -y @example/notes-mcp" --kind mcp --as remote-notes
intel connect https://example.com/mcp --kind mcp --as docs
intel connect --from ./.mcp.json
```

The target is the command that starts the server, or its URL. `--from` takes a server you
already registered for Claude Code or VS Code, with its `env` and `cwd`, and records it as an
ordinary source; name one with `intel connect <server> --from <file>` when the file holds several.

A server's tools are named `source.tool`, so two servers can both offer `search`. A server that
lists resources is searchable too: search matches each resource's name and description, and
`get` reads one by its URI. `/tools` in the shell shows each tool's declared effect.

**Credentials for a server.** The server starts with a handful of variables, such as `HOME` and
`PATH`, and nothing else. Name what it needs in the source's `env` option, which a deployment or
an imported `.mcp.json` sets: a bare `NAME` forwards your value, from the environment or a `.env`,
and is refused when unset; `NAME=value` passes a literal. A server that fails to start shows its
own error.

**A store.** A wiki or a records store is a server of its own package:

```bash
intel connect "latent-wiki serve --deployment ~/wikis/research" --kind mcp --as wiki
```

Its tools come from the server, and its pages are searchable as resources.

## Remote locations

A `files` or `code` target may be a URI such as `s3://bucket/notes`, read through `fsspec` with
the credentials your environment already has ([credentials](../deployments.md#credentials)). An
MCP server is remote when its target is a URL.

## By name, from a registry

A registry file lists the stores on a machine, so a source can be attached by id:

```bash
intel connect notes-vault
intel connect notes-vault --remote      # the entry's mirror, not its local copy
```

The registry is the deployment's `registry:`, else `KNOWLEDGE_REGISTRY`, else
`~/.config/latent-intel/registry.yaml`. `intel stores` lists what is attached and what the
registry offers. A source attached by id is recorded by id, and found again on each machine.

## What is recorded, and detaching

`connect` records the source in your config, under the active deployment, so later commands and
the shell attach it too. It never edits the deployment file. Switching deployment leaves it
behind: a source attached with no deployment active belongs to none.

```bash
intel stores
intel disconnect notes
```

A source that fails to attach on a later command is reported on stderr and skipped; the rest
still attach.

## Known limits

- **A relative path is recorded as typed,** and resolved against the working directory of each
  later command. Give an absolute path or one starting `~`.
- **`disconnect` removes only what you attached.** A source the deployment declares is reported
  as not attached; leave it out with `detached:` in your config by hand
  ([your config](../deployments.md#your-config)).
- **No source filters by type:** `search --kind` is ignored by `files` and `code`, and refused
  by `mcp`.
