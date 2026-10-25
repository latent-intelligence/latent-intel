# Choose a runtime

`ask` needs a runtime, who runs the agent loop, and most runtimes need a host, the endpoint they
reach a model through. This guide picks one, sets it up and checks it, without spending anything
until the first question. The tables of runtimes, hosts and options: [runtimes](../runtimes.md).

## Which one

| You have | Choose |
|---|---|
| Claude Code installed and signed in | `claude-cli`: no key; it uses your Claude Code plan |
| An Anthropic API key | `anthropic-sdk` or `custom` with host `anthropic` |
| Claude on Azure AI Foundry | `anthropic-sdk`, whose default host is `foundry-anthropic` |
| One key for models from many vendors | `custom` with host `openrouter` |
| A model server on your own machine | `custom` with host `local`: nothing leaves the machine |
| A wish for Claude Code's own harness, or subagents | `claude-agent-sdk`, an extra |
| A dollar budget per session, and versioned agents | `claude-managed-agents` ([use managed agents](use-managed-agents.md)) |
| The OpenAI Agents SDK | `openai-agents`, an extra, on any chat host |

The sources, tools and citations are the same on every runtime, so switching later is a line of
config. `claude-cli` reaches a `files` or `code` source through a server this package starts
for it; the others call every source's tools in this process.

## See what this machine could reach

`intel hosts` lists every host each runtime can reach, and what each still needs. Captured on a
machine with nothing set:

```
$ intel hosts
anthropic-sdk
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
…
claude-cli — no hosts
…
custom
  ! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  …
  · openrouter         set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
…
Names of variables, never values — safe to paste. Set them in a `.env` beside the deployment file,
or export them.
```

`claude-cli — no hosts` is expected: the binary has chosen its endpoint already. `custom` has no
default host, which the `!` line says. On the `foundry-openai` row (cut from this capture),
`(or ANTHROPIC_FOUNDRY_API_KEY)` names the variable read when its own is unset.

## Set it up

Take OpenRouter as the example. Put the key in the environment or a `.env`
([credentials](../deployments.md#credentials)), and the row flips:

```
$ export OPENROUTER_API_KEY=…
$ intel hosts
custom
  ! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  …
  ✓ openrouter         OPENROUTER_API_KEY
```

Choose the runtime, host and model, in the shell or your config. Each shell command is saved at
once, and prints what is still missing:

```
latent › /runtime custom
latent › /host openrouter
latent › /model anthropic/claude-sonnet-4.5
```

```yaml
# ~/.config/latent-intel/config.yaml
runtime: custom
runtimes:
  custom:
    host: openrouter
    model: anthropic/claude-sonnet-4.5
```

A deployment can carry the same under `agent:`, and yours wins over it
([agent](../deployments.md#agent)). Model ids are the host's: `<vendor>/<model>` on OpenRouter, a
deployment name on Foundry and Azure, an id from the [models page](https://docs.anthropic.com/en/docs/about-claude/models)
on `anthropic`. A chat host has no default model, so between `/host` and `/model` the runtime
reports `no model is set`.

## Check it

```
$ intel hosts
custom ← configured
  …
  ✓ openrouter         ← in use  OPENROUTER_API_KEY
  …

$ intel doctor
runtimes
  custom loop
    ✓ custom ← configured
  sdk runner
    · anthropic-sdk — set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  …
  model: anthropic/claude-sonnet-4.5
```

Captured with a placeholder key: neither command calls the host, so a ✓ means the variables are
set, not that the key works. The first `ask` is the first request, and the first cost. A 401
names the variable to check, a 404 most often the model id.

## Other hosts

- **`anthropic`:** `ANTHROPIC_API_KEY`, then `/runtime anthropic-sdk` and `/host anthropic`. The
  default model is `claude-sonnet-5`.
- **`foundry-anthropic`:** `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_RESOURCE`, then
  `/runtime anthropic-sdk`. The model is your Foundry deployment's name.
- **`local`:** `LOCAL_OPENAI_BASE_URL`, such as `http://localhost:11434/v1` for Ollama, then
  `/runtime custom`, `/host local` and `/model` with a model the server has. Choose a model that
  supports tool calls, or the agent reaches none of your sources.
- **`claude-cli`:** Claude Code on your `PATH`, then `/runtime claude-cli`. `/model` takes Claude
  Code's names, such as `sonnet`.

Every host's variables: [the hosts](../runtimes.md#the-hosts).

## Known limits

- **`claude-cli` without the `claude` binary** is reported by `intel doctor`, and by `ask`, only
  as installed but unable to run here, without saying that the binary is missing.
- **`ask` with no runtime** points at `~/.config/latent-intel/config.yaml` even when
  `LATENT_INTEL_HOME` moves your config.
