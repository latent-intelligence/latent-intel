# Getting started

Five minutes, three shapes of use. The full reference is
[`user-guide.md`](user-guide.md) — come here first, go there when a question outlives
this page.

- [Install](#install)
- [One source, right now](#one-source-right-now)
- [The shell](#the-shell)
- [A project — several sources, plus branding](#a-project--several-sources-plus-branding)
- [Where state lives](#where-state-lives)
- [Next](#next)

---

## Install

```bash
uv tool install git+https://github.com/latent-intelligence/latent-intel
uv tool install git+https://github.com/latent-intelligence/latent-intel --reinstall
```

From a clone instead — if you are working on the engine itself — `uv tool install .`.

That is the whole install — no extras, no sibling package. Files, MCP servers and wikis,
local or `s3://`, all work from it, because a published wiki store is a versioned format
(`manifest.json` plus `pages/`) that the client reads directly. A wiki compiler is what
you install to *build* a wiki, not to read one, and it is added alongside the engine
(`--with <wiki-compiler>`) only on the machine that authors one.

Check it:

```
$ intel doctor
latent-intel 0.1.0

connectors (entry points)
  ✓ files
  ✓ mcp
  ✓ wiki
  · vector — not installed
```

### Credentials

Nothing to set, normally. Object storage uses `fsspec` and `botocore`, so `aws
configure`, `AWS_PROFILE`, SSO and instance roles work exactly as they already do. This
package holds no credential code.

On a machine with none of that configured, a `.env` beside the project file supplies
them:

```bash
# ~/deployments/research/.env       (git-ignored, never committed)
AWS_ACCESS_KEY_ID=…
AWS_SECRET_ACCESS_KEY=…
AWS_DEFAULT_REGION=us-east-1
```

`~/.config/latent-intel/.env` is the machine-wide equivalent; the project's file wins
where both set a variable, and **anything already exported beats both** — so a one-off
`AWS_PROFILE=other intel search …` still does what you mean.

Those are the only two places. The working directory and the root of a checkout are
never read, so a `.env` left there is silently ignored — `intel doctor` shows which file
was loaded, or the two paths it looked in when none was.

Credential *values* never reach a command line: a served source is a subprocess handed
the file's path, not its contents.

**Working on the package itself?** `uv sync --extra dev`, then prefix everything below
with `uv run`.

---

## One source, right now

This section and the next are **two ways in, not two steps.** Attach sources one at a
time as below, or describe them all in a project file ([below](#a-project--several-sources-plus-branding)).
A project needs nothing from this section, and a real deployment usually starts there.

Point at a store and search it. Nothing else is needed — object storage uses whatever
credentials your environment already has.

```bash
intel connect s3://<bucket>/wikis/design --kind wiki --as design
intel search "context collapse"
intel get design:context-collapse
```

```
$ intel connect s3://<bucket>/wikis/design --kind wiki --as design
✓ wiki   design  148 pages  search fetch tools    built 2026-08-24
recorded in ~/.config/latent-intel/config.yaml
```

`--kind` is required for a path or URI: guessing it from a directory's contents would be
wrong exactly when it matters. `built` is the manifest's date — a store nobody has
rebuilt in a month can say so.

Attached sources persist, because the CLI is a new process every time. `intel stores`
lists them, `intel disconnect design` removes one.

Other kinds, same shape:

```bash
intel connect ~/knowledge/raw --kind files --as notes
intel connect "npx -y @upstash/context7-mcp" --kind mcp --as ctx7
```

---

## The shell

Bare `intel` opens it, and it is the primary way in.

```
latent › search progressive disclosure
latent › /use design
latent › open progressive-disclosure
latent › /tools
```

One rule of grammar: **a leading `/` is session management, anything else is a query.**
History persists between sessions, tab completes commands, source ids and the refs from
your last search, and Ctrl-D leaves.

---

## A project — several sources, plus branding

A project is one YAML file describing a deployment: its sources, its branding, its saved
commands. The engine is installed once and reads projects; it never writes them.

You do not need to have attached anything above first — a project declares its own
sources, and switching to one attaches all of them at once.

```yaml
# ~/deployments/research/research.yaml
name: research
title: Research Intelligence

branding:
  name: RESEARCH
  tagline: standing context, with provenance

sources:
  - id: design
    kind: wiki
    target: s3://<bucket>/wikis/design
    options:
      context: s3://<bucket>/li/knowledge/context   # the material the wiki summarises
  - id: notes
    kind: files
    target: ~/knowledge/raw
```

```bash
intel project add ~/deployments/research   # finds it in place; nothing is copied
intel project use research
intel                                    # branded shell, both sources attached
```

Three things worth knowing, and they are the ones that bite otherwise:

- **Relative paths resolve against the project file**, never your working directory — so
  a project repo checked out anywhere works there.
- **`context:` pairs a wiki with what it summarises.** Optional: leave it out and search
  and `open` are unaffected, only escalation to a page's underlying source has nothing to
  read.
- **`intel connect` never edits the project file.** It records into your own overlay for
  the active project, so a checked-out deployment repo stays clean.

Copy `src/latent_intel/data/projects/latent.yaml` to start — it is the worked example,
with every field commented in the order the loader reads them.

Verify before trusting it:

```bash
intel project validate research   # what is wrong with it, without switching
intel project show             # every resolved value, and which layer it came from
```

---

## Where state lives

```
~/.config/latent-intel/config.yaml     the active project, your overlays, preferences
~/.local/share/latent-intel/           history and caches
LATENT_INTEL_HOME                      overrides both — a throwaway setup for trying things
```

Credentials never go in the config file; runtimes read the environment.

---

## Next

- [`user-guide.md`](user-guide.md) — every command, `--json` scripting, MCP servers,
  what to do when something is wrong.
- `intel --help`, and `intel <command> --help`.
