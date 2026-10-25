# Shape the agent

A deployment shapes how the agent behaves with three kinds of Markdown file: a **persona** sent
on every turn, **skills** loaded when a question needs one, and **subagents** it can hand work
to. Together they are the deployment's context engineering. They follow Claude Code's file
conventions, so one folder can serve this package, Claude Code and the Agent SDK. Each is
declared in the deployment ([keys](../deployments.md#top-level)): a `skills/` or `agents/` folder
loads only when its key names it, and the file `intel deployment new` writes has those keys
commented out.

```yaml
agent:
  persona: ./persona.md
  persona_mode: append
skills: ./skills
agents: ./agents
```

## Persona

What is true on every turn: the agent's role, its voice, what it refuses, how it cites. One file,
sent whole with every question.

```markdown
You answer questions about context engineering from the attached notes and papers. Quote the
passage you rely on, and give its ref. When two sources disagree, say so and cite both.
```

The agent is always told which sources are attached, and to cite what it uses as `source:key`
and say when the sources do not answer. `persona_mode: append`, the default, adds your persona
after that instruction; `replace` puts it in the instruction's place. The list of sources is never
replaced.

## Skills

What is true only sometimes, and too long to send every time: how a corpus is organised, query
patterns that work on it, a method to follow when asked. A skill is `skills/<name>.md` or
`skills/<name>/SKILL.md`; from a folder only `SKILL.md` is read.

```markdown
---
name: citation-style
description: How to cite a page or a range of pages
---
Cite a single page inline as `source:key`, in the sentence that uses it. For a range, cite the
first and last page, and say what the range covers.
```

**The agent loads a skill when it needs one.** It sees each skill's name and description, and
reads the body with the `load_skill` tool. The description is `description:`, or the body's first
line, with `when_to_use:` added. Under `claude-cli`, which cannot reach `load_skill`, every skill
body is sent whole instead.

**You can run one by hand.** In the shell, `/citation-style how do I cite pages 4 to 9?` sends the
skill's body with your question, and keeps it for the rest of the session. `$ARGUMENTS` in the
body is replaced with what you typed; a body without it takes your text as the question.

| Frontmatter | The agent may load it | `/name` runs it |
|---|---|---|
| none | yes | yes |
| `disable-model-invocation: true` | no | yes |
| `user-invocable: false` | yes | no |

Differences from Claude Code: a skill the agent loads is not carried into the next turn, so it
loads again when needed, and only `$ARGUMENTS` is replaced, not `$1` or `$ARGUMENTS[0]`. Keys this
package does not act on, such as `allowed-tools` or `model`, are reported rather than dropped.

## Subagents

A delegate with its own context: the main agent hands it a task and gets back its report, not its
working. Use one when the trail would crowd the conversation, when parts of a question can be
worked separately, or when the task should have fewer tools. One file per subagent in `agents/`:

```markdown
---
name: reviewer
description: Checks a draft answer against the pages it cites. Use before answering from more
  than three sources.
tools: notes.files_search, notes.files_get
model: haiku
---
Check the draft against its citations. Open each cited page, say which claims it supports and
which it does not, and return that as a short list.
```

`name`, `description` and a body are required. `tools` names tools as `/tools` prints them,
`source.tool`, or the web tools a scope turns on, `WebSearch` and `WebFetch`; without it the
subagent may use everything the main agent can. `disallowedTools` takes the same names. `model` is
an alias such as `haiku` or a full model name. A file that sets `permissionMode` is refused; other
Claude Code keys are reported and ignored.

**Only `claude-agent-sdk` runs subagents,** and only the ones declared here: Claude Code's
built-in subagents are not offered. On any other runtime `intel doctor` warns that the deployment
declares subagents it will not run. Under `claude-agent-sdk`, it names any tool a subagent lists
that it would not be given, such as a source not attached or a writing tool withheld, and that
tool is dropped.

## Check it

```bash
intel deployment validate     # files that do not parse, missing fields, keys not acted on
intel doctor                  # subagents the runtime will not run, and tools that resolve to nothing
```

Each bad file is reported and skipped, so the others still load. To compare one version of a
persona or a skill with another on real questions, record both as variants of an
[eval batch](run-an-eval-batch.md): each recording's header carries a hash of the persona, skills
and subagents in force.
