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
├── skills/                      name.md or name/SKILL.md  →  loaded when a question needs one
│   └── methodology.md
├── agents/                      one .md per subagent  →  delegates, claude-agent-sdk only
│   └── reviewer.md
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

Persona, skills and [subagents](#subagents) are the deployment's **context
engineering**: markdown, not code, that shapes what the agent knows and how it behaves.

A persona and skills are both given to the agent before your question, and the line
between them is what is true *when*. A **persona** is true on every turn — the role,
what it refuses, how it cites — so it is one file and it is always sent. A **skill** is
true *sometimes* and too long to pay for always: how this corpus is organised, the query
patterns that work on it, a method it should follow when asked for one.

```yaml
# ~/deployments/research/research.yaml
skills: ./skills                      # name.md or name/SKILL.md, in name order
agent:
  persona: ./persona.md               # resolved against this file, like every path
  persona_mode: append                # or: replace
```

`persona_mode: append` (the default) adds your persona after ours. `replace` puts it in
place of our posture line — *"cite what you use as `source:key`, say when the sources do
not answer"* — and nothing else: the list of attached sources is never replaced, because
the agent cannot use its tools without it. An unrecognized mode is reported by
`intel project validate` and read as `append`.

A skill is either a flat `name.md` or a folder `name/SKILL.md` — the layout Claude Code
and the Agent SDK read, so one skills folder serves both. Only `SKILL.md` is read from a
folder. A skill is named by the file or folder, or by a `name:` in its frontmatter:

```markdown
---
name: citation-style
---
Cite a single page inline as `source:key`, in the sentence that uses it.
```

**Skills load on demand, as in Claude Code.** The agent sees one line per skill — its
name and description — and loads the body with `load_skill` when a question needs it.
The description is `description:`, else the body's first line, with `when_to_use:`
appended. `claude-cli` cannot reach `load_skill`, so it is given the bodies instead.

In the shell, **`/name [question]` runs a skill by hand**: its body stays in the prompt
for the rest of the session, with `$ARGUMENTS` replaced by the text after the name. A
built-in beats a skill and a skill beats a project command; either clash is reported.

| Frontmatter | The agent may load it | `/name` runs it |
|---|---|---|
| *(none)* | yes | yes |
| `disable-model-invocation: true` | no | yes |
| `user-invocable: false` | yes | no |

Two differences from Claude Code: a skill the agent loads itself is not carried into
the next turn (it loads again when needed), and only `$ARGUMENTS` is substituted.
Frontmatter this build does not act on — `allowed-tools`, `model`, `context` and the
like — is reported, not silently dropped. A leading `---` block without any of Claude
Code's skill keys is prose, not frontmatter.

A missing persona file, a skill whose frontmatter will not parse, a folder with no
`SKILL.md`, and a second skill with a name already taken are each **reported and
skipped** — one bad file does not cost a deployment its other four. `intel project
validate` lists them.

---

## Subagents

A **subagent** is a delegate with its own context. The main agent hands it a task and
gets back its report, not its working — which is the one thing a subagent buys that a
skill does not. Reach for one when the trail would crowd the conversation (twenty
searches to produce one table), when parts of a question can be worked independently,
or when the task should get fewer tools than the main agent has.

```yaml
# ~/deployments/research/research.yaml
agents: ./agents                      # every *.md in it, in filename order
```

The files are in Claude Code's format, so the same folder serves Claude Code and the
Agent SDK unchanged:

```markdown
---
name: reviewer
description: Checks a draft answer against the pages it cites. Use before answering
  a question that rests on more than three sources.
tools: design.search, design.fetch
model: haiku
---
You check a draft against its citations. Open each cited page, say which claims it
supports and which it does not, and return that as a short list.
```

`name`, `description` and a body are required. `tools` names tools the way `/tools`
prints them — `source.tool` — or a web tool the runtime has been told to supply; leave
it out and the subagent may use everything the main agent can. `disallowedTools` takes
the same names and is honoured. `model` is optional, an alias such as `haiku` or a full
model name. A file that sets `permissionMode` is refused rather than loaded — the
runtime's own permission policy decides, and ignoring the key could only widen what the
subagent may do. Other Claude Code keys are reported and ignored.

**Only `claude-agent-sdk` runs them**, and only the ones you declare: Claude Code's own
built-in subagents are not offered. Under any other runtime they are not sent, and
`intel doctor` says so — along with any tool a subagent names that the runtime would not
give it: a source not attached, a writing tool withheld under `approval: ask`, a web tool
not turned on, a typo. A file missing a required field is reported and skipped.

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
