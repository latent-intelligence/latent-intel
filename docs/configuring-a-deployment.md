# Configuring a deployment

A **deployment** is one directory, committed to whatever repo you keep it in. The engine
reads it and never writes to it, so a checked-out deployment repo stays clean on first
use — `intel connect` records into *your* overlay instead.

Standing up the next deployment is another directory like this one. No code change, no
release.

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
├── skills/                      one .md per skill  →  the agent's standing knowledge
│   └── methodology.md
└── persona.md                   voice and standing instructions
```

**Every relative path resolves against `research.yaml`**, never your working directory, so
the same checkout works on any machine and in any cwd. Remote locations go through
`vars:`, which an environment variable of the same name overrides — a deployment points at
its own bucket without editing the committed file.

Credentials are the one thing that does not live in the project file. Where they do live,
and why only those two places, is in
[getting-started](getting-started.md#credentials).

---

## Branding

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
Latent defaults.

---

## Saved commands

Same idea: one markdown file per command, in the directory the project names.

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
which is why they need no code and no release. The two points where new *capability*
plugs in — connectors and tools — are in [`../CLAUDE.md`](../CLAUDE.md).

---

## Persona and skills

Both are markdown the agent is given before your question, and the line between them is
what is true *when*. A **persona** is true on every turn — the role, what it refuses,
how it cites — so it is one file and it is always sent. A **skill** is true *sometimes*
and too long to pay for always: how this corpus is organised, the query patterns that
work on it, a method it should follow when asked for one.

```yaml
# ~/deployments/research/research.yaml
skills: ./skills                      # every *.md in it, in filename order
agent:
  persona: ./persona.md               # resolved against this file, like every path
  persona_mode: append                # or: replace
```

`persona_mode: append` (the default) adds your persona after ours. `replace` puts it in
place of our posture line — *"cite what you use as `source:key`, say when the sources do
not answer"* — and nothing else: the list of attached sources is never replaced, because
the agent cannot use its tools without it. An unrecognized mode is reported by
`intel project validate` and read as `append`.

A skill is named by the filename, or by a `name:` in its frontmatter when you want
another name: A leading `---` block counts as frontmatter only when it is a mapping with a `name:`; otherwise the file is prose from its first line, and nothing is consumed.

```markdown
---
name: citation-style
---
Cite a single page inline as `source:key`, in the sentence that uses it.
```

Skills are sent whole, so keep them to what earns its tokens. A missing persona file or
a skill whose frontmatter will not parse is **reported and skipped** — one bad file does
not cost a deployment its other four. `intel project validate` lists them.

---

## Checking it

```bash
intel project validate research   # what is wrong with it, without switching
intel project show                # every resolved value, and which layer set it
```

**Our own branding takes the client path.** The engine's identity is a project file like
any other — `src/latent_intel/data/projects/latent.yaml` — so the path you use is the one
we use, and it cannot rot unnoticed. Copy that file to start: every field is shown in it,
commented, in the order the loader reads them.
