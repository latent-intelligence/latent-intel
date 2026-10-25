# Use the shell

Bare `intel` opens an interactive shell: search, read and ask from one prompt, with the attached
sources held open between commands. It reads the same config as the CLI, so a source attached in
one is attached in the other.

## What you type

One rule: **a line starting with `/` manages the session; anything else is a query.**

| You type | What happens | Costs |
|---|---|---|
| `search context collapse` | every attached source searched, one list per source | nothing |
| `open notes:context-collapse.md` | one document in full; `get` is the same | nothing |
| `what causes context collapse?` | the agent searches and reads for you, then answers | a model turn |
| `ask search strategies for long contexts` | the same, for a question that starts with a verb | a model turn |

A line starting with `search`, `open`, `get` or `ask` is read as that verb, so a question that
begins with one of those words needs `ask` in front. A near miss, such as `serch …`, is caught
and the correction suggested, never made for you. `open` with a bare key reads from the current
source: `/use notes` sets it.

The shell keeps one conversation: a follow-up question sees the earlier ones. Leave with `/exit`
or Ctrl-D; Ctrl-C abandons only the line you are typing.

## Slash commands

| Command | Does |
|---|---|
| `/sources` | What is attached |
| `/connect <store> [--kind k] [--as id] [--remote]` | Attach one, and record it, as `intel connect` does |
| `/disconnect <source>` | Detach one, and forget it if you attached it |
| `/use [source]` | Which source a bare key reads from |
| `/tools` | Every attached tool, with its declared effect |
| `/record [file \| off] [--purpose p] [--title t]` | Record every event to a file ([script and record](script-and-record.md)) |
| `/replay <file> [--since N]` | Render a recording |
| `/deployment [name]` | Which deployment is active; with a name, switch to it |
| `/runtime [name]`, `/model [name]`, `/host [name]` | Which runtime answers, its model, its host |
| `/hosts` | Every host, and what each needs, as `intel hosts` prints it |
| `/scope [off\|search\|browse] [--allow a,b \| --block a,b \| --any]` | The web scope, for this session only ([web access](ask-with-web-access.md)) |
| `/clear`, `/help`, `/exit` | Clear the screen, list the commands, leave |

`/runtime`, `/model` and `/host` are saved in your config at once, the model and host per
runtime, and each prints what is still missing, if anything. Bare, each prints the value in
force, wherever it came from. `none` clears your setting; a value the deployment declares then
applies again. [Choose a runtime](choose-a-runtime.md) uses them.

`/deployment <name>` saves the choice, but the sources already open stay open: restart the shell
to attach the new deployment's. It also drops a `/scope` set for the deployment you left.

## A deployment's own commands

The active deployment's saved commands and skills are slash commands too, listed by `/help`
under "this deployment".

```
research › /brief context collapse
research › /citation-style how should I cite a page range?
```

A saved command runs the searches or questions it declares. A skill's text joins the
conversation for the rest of the session, with what you typed after its name as its argument
([skills](shape-the-agent.md#skills)). A built-in wins over both, and a skill over a command of
the same name. The shell reports a skill a built-in hides when it starts, and
`intel deployment validate` a skill that hides a command; a command a built-in hides is dropped
without a word. `intel run <name>` runs a saved command from the command line, whatever its name.

## Editing

History is kept between sessions, in `~/.local/share/latent-intel/history`. Tab completes slash
commands, source ids after `/use`, `/connect` and `/disconnect`, and the refs from your last
search after `open`. Output is ordinary scrollback: scroll, select and copy as in any terminal.

The banner shows what the attached sources can do and how many items they hold, not their
names. A deployment's branding changes the logo, tagline, prompt and colours
([branding](../deployments.md#branding)).
