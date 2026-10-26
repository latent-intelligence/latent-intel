# Runtimes and hosts

Every runtime, every host, what each needs and the options each takes. This page is the only
home for these tables; [choose a runtime](guides/choose-a-runtime.md) walks through picking one.

A **runtime** is who runs the agent loop: who asks the model again and who runs the tools it
asks for. A **host** is the endpoint a runtime talks to, with its own credentials and its own
way of naming models. `intel hosts` prints every host with what it still needs, and
`intel doctor` prints every runtime with the reason it cannot run, if any.

## The runtimes

| Runtime | Loop run by | Install | Hosts | Sees your sources as |
|---|---|---|---|---|
| `custom` | this package | base | all seven | in-process tools |
| `anthropic-sdk` | the Anthropic SDK's tool runner | base | `anthropic`, `foundry-anthropic` | in-process tools |
| `openai-agents` | the OpenAI Agents SDK | `[openai-agents]` | the five chat hosts | in-process tools |
| `claude-agent-sdk` | Claude Code, through the Claude Agent SDK | `[claude-agent-sdk]` | `anthropic`, `foundry-anthropic` | in-process tools |
| `claude-cli` | the `claude` binary on your machine | base, plus Claude Code | none: the binary chose its endpoint | MCP servers |
| `claude-managed-agents` | Claude Managed Agents, on Anthropic's servers | base | `anthropic` | tools called back over the session |

`intel doctor` groups them by who runs the loop: `custom loop` (`custom`), `sdk runner`
(`anthropic-sdk`, `openai-agents`, `claude-agent-sdk`), `delegated` (`claude-cli`) and
`managed` (`claude-managed-agents`).

- **In-process tools** means every attached source's tools go through this package's router,
  with the same tool events and the same approval rule on every runtime.
- **`claude-cli`** reaches an `mcp` source as that server, and a `files` or `code` source
  through a server this package starts for it. Under this runtime `intel doctor` marks
  `(not visible to the agent)` a source no server reaches, and on Windows every source while
  one has `cwd`, since `ask` then refuses the turn. A server Claude Code could not start is
  reported by `intel ask` before the answer, with Claude Code's own status (`failed`,
  `needs-auth`), and the turn goes on; when none of the turn's servers started and the web
  tools are off, the turn ends there with an error naming them. A server still starting
  when the turn begins is not checked. It never gets
  `load_skill`, so it is given the skill bodies whole. Claude Code ignores an `mcp` source's
  `cwd`, so this package starts that server through `/bin/sh -c 'cd "$0" && exec "$@"'`; on
  Windows, with no `/bin/sh`, `intel ask` refuses a source with `cwd`. Killing `intel` with SIGTERM or SIGHUP ends the `claude` it started; a
  signal `intel` was started ignoring, as under `nohup`, stays ignored.
- **`claude-agent-sdk`** runs with your own Claude Code setup shut out: no `~/.claude`
  settings, `CLAUDE.md`, memory, skills or MCP servers. It authenticates with the host's
  variables only, never a claude.ai login. A follow-up resumes its session, which needs
  Claude Code's session files under `~/.claude/projects/`. No Windows wheel is published.
- **`openai-agents`** turns the SDK's tracing off. An answer cut short by the output limit
  completes rather than failing, unlike `custom`.
- **`anthropic-sdk`**, **`openai-agents`** and **`claude-agent-sdk`** answer a tool name the
  model made up without a tool event; `custom` reports it as a failed tool call.

## The hosts

One table, whichever runtime dials it. Variables are read from the environment, or a `.env`
([where `.env` is read](deployments.md#credentials)); never from a config file.

| Host | Protocol | Needs | Optional | Model is | Default model |
|---|---|---|---|---|---|
| `anthropic` | messages | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` | a published model id | `claude-sonnet-5` |
| `foundry-anthropic` | messages | `ANTHROPIC_FOUNDRY_API_KEY`, and one of `ANTHROPIC_FOUNDRY_RESOURCE` or `ANTHROPIC_FOUNDRY_BASE_URL` | — | a Foundry deployment name | `claude-sonnet-5` |
| `openai` | chat | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | a published model id | none |
| `foundry-openai` | chat | `FOUNDRY_API_KEY`, and one of `FOUNDRY_RESOURCE` or `FOUNDRY_BASE_URL` | — | a Foundry deployment name | none |
| `openrouter` | chat | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` | `<vendor>/<model>` | none |
| `azure-openai` | chat | `AZURE_OPENAI_API_KEY` or `AZURE_OPENAI_AD_TOKEN`, `AZURE_OPENAI_ENDPOINT`, `OPENAI_API_VERSION` | — | an Azure deployment name | none |
| `local` | chat | `LOCAL_OPENAI_BASE_URL` | `LOCAL_OPENAI_API_KEY` | whatever the server serves | none |

- **A resource name or a base URL, never both.** Setting both for one host is reported.
- **`foundry-openai` falls back** to `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_RESOURCE`
  where its own pair is unset. `ANTHROPIC_FOUNDRY_BASE_URL` does not stand in: it is the other
  surface's address.
- **`azure-openai` with both credentials set** uses the token.
- **`local`** is any OpenAI-compatible server on your machine, such as Ollama, vLLM or
  LM Studio. Without `LOCAL_OPENAI_API_KEY` it sends the placeholder `local`.
- **A variable set to the empty string** is reported as set but empty.
- **Foundry takes deployment names,** not dated model ids: `claude-sonnet-5` works where a name
  with a date on the end returns a 404.

No model name is checked here; an unknown one fails when you ask, with the host's own message.
Model ids and prices are the provider's: [Anthropic](https://docs.anthropic.com/en/docs/about-claude/models),
[OpenRouter](https://openrouter.ai/models).

## Options

Set under `runtimes: <runtime>:` in your config, or under `agent: runtimes:` in a deployment
([keys](deployments.md#agent)). An option a runtime does not take is refused by name. `approval:`
there is reported and ignored: approval is set at the top level.

| Option | `custom` | `anthropic-sdk` | `openai-agents` | `claude-agent-sdk` | `claude-cli` | `claude-managed-agents` |
|---|---|---|---|---|---|---|
| `host` | none: required | `foundry-anthropic` | `openai` | `foundry-anthropic` | — | `anthropic` only |
| `model` | the host's default; required on a chat host | `claude-sonnet-5` | required | `claude-sonnet-5` | the binary's own | `claude-sonnet-5` |
| `max_tokens` | 16384 | 16384 | 16384 | — | — | — |
| `max_tool_rounds` | 10 | 10 | 10 | 10 | — | refused |
| `max_cost_usd` | refused | refused | refused | optional | refused | optional |
| `tokens_param` | chat hosts only | — | yes | — | — | — |
| `include_usage` | chat hosts only; `true` | — | `true` | — | — | — |
| `cwd` | — | — | — | where the binary runs | where the binary runs | — |
| `cli_path` | — | — | — | a `claude` binary instead of the bundled one | — | — |
| `command` | — | — | — | — | `claude` | — |
| `effort` | — | — | — | — | — | sent with the model |
| `agent_version` | — | — | — | — | — | pins a synced version |

- **`host`** comes from `/host` or `host:`, then from the runtime's own environment variable:
  `LATENT_INTEL_CUSTOM_HOST`, `LATENT_INTEL_ANTHROPIC_SDK_HOST`, `LATENT_INTEL_OPENAI_AGENTS_HOST`
  or `LATENT_INTEL_CLAUDE_AGENT_SDK_HOST`, then the default above.
- **`tokens_param`** names the output-token parameter on a chat host: `max_completion_tokens`
  by default, `max_tokens` on `openrouter` and `local`.
- **`include_usage: false`** stops a chat request asking for token usage, for an Azure API
  version that rejects `stream_options`. Usage then has no token counts. `openai-agents`
  refuses it on `api.openai.com`, where the SDK asks regardless.
- **`claude-agent-sdk` on `foundry-anthropic`** sets `CLAUDE_CODE_USE_FOUNDRY` for the binary.
  Claude Code's `ANTHROPIC_DEFAULT_*_MODEL` variables pass through.
- **`claude-managed-agents`** also keeps what `intel managed sync` created, in your config
  ([use managed agents](guides/use-managed-agents.md)).

## Ceilings

Two options bound one question.

| Option | Stops a question | On |
|---|---|---|
| `max_tool_rounds` | after that many model round-trips; `none` for no ceiling, and 0 is refused | `custom`, `anthropic-sdk`, `openai-agents`, `claude-agent-sdk` |
| `max_cost_usd` | at that many US dollars, above zero | `claude-agent-sdk` (the binary's own estimate), `claude-managed-agents` (the session's budget) |

A question stopped by a ceiling keeps its answer so far and says why it stopped. Under `custom`,
the round ceiling sends one last request with no tools allowed, so the model can answer from
what it has; the other runtimes end with the text already written. A ceiling reached before any
text is a failure. A stopped answer still exits 0, and its `agent_completed` event carries
`stopped: tool_rounds` or `stopped: cost` ([recording](contracts/recording.md#events)).

## The web

What each runtime makes of `web: search` and `web: browse`
([ask with web access](guides/ask-with-web-access.md)). A runtime that cannot honour the scope in
force refuses the question rather than answering without the web.

| Runtime | `search`, `browse` become | Domain lists | `max_uses` |
|---|---|---|---|
| `custom`, `anthropic-sdk` on `anthropic` | the API's `web_search`, plus `web_fetch` (`_20260209`) | yes | yes |
| `custom`, `anthropic-sdk` on `foundry-anthropic` | `web_search_20250305`, plus `web_fetch_20250910` | yes | yes |
| `custom` on a chat host, `openai-agents` | refused: only `off` | — | — |
| `claude-agent-sdk`, `claude-cli` | Claude Code's `WebSearch`, plus `WebFetch` | refused | refused |
| `claude-managed-agents` | the service's `web_search`, plus `web_fetch` | yes | refused |

## Approval

No runtime can stop mid-answer to ask you, so which tools the agent is offered is decided before
the question, from each tool's declared effect. Under `approval: auto` every tool is offered.
Under any other value, `ask` (the default) or `never`, a tool that writes is withheld:
`local_write`, `external_write` or `destructive`. A tool that declares no effect counts as
`external_write`, and `intel doctor` lists it.
