# Configuring compute

Sources say where context comes from. A **runtime** says where answers come from, and
every runtime but one reaches a **host** — an endpoint with its own credentials, its own
address, and its own way of naming models.

`intel hosts` lists every host with what it still needs. This page is one section per
host, in the order most people want them: what it is for, what to set, and what you
should see.

---

## The Anthropic API

**What it is for.** Claude from Anthropic directly, with no gateway in between. This is
the one to use when the deployment has its own Anthropic account, and the shortest path
to a capable model that handles a long tool loop well.

Unlike OpenRouter, this is the **`anthropic` host row** — a different protocol, not just
a different endpoint. A runtime is **who owns the agent loop**, and `custom` owns ours
for every row; the row says which endpoint and which protocol. `sdk-anthropic` is the
SDK-run sibling for this row: same protocol, same hosts, same variables, so everything on
this page applies to it unchanged.

### 1. Set one variable

```bash
# ~/.config/latent-intel/.env
ANTHROPIC_API_KEY=sk-ant-…
```

`ANTHROPIC_BASE_URL` is optional, and only for a proxy in front of the API.

### 2. Check it

```
$ intel hosts

custom
  ! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  ✓ anthropic          ANTHROPIC_API_KEY
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  · openrouter         set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
```

`custom` has **no default host** — seven rows across two protocols make any default right
for at most one deployment — so a key alone is not enough: the row has to be chosen as
well, which is the next step and which the `!` line above says in as many words. The `✓`
names the variable answering for it, so you can confirm the key landed before going
further.

### 3. Choose it

```
latent › /runtime custom
latent › /host anthropic
latent › /model claude-haiku-4-5
```

Or in config:

```yaml
runtime: custom
runtimes:
  custom:
    host: anthropic
    model: claude-haiku-4-5
```

**Model ids are Anthropic's own**, published at
[docs.anthropic.com](https://docs.anthropic.com/en/docs/about-claude/models) — not
`<vendor>/<model>`, which is OpenRouter's convention:

| model | id | context | $/1M in | $/1M out |
|---|---|---|---|---|
| Claude Opus 5 | `claude-opus-5` | 1M | $5.00 | $25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | $2.00 | $10.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

All three handle tool calls. **Haiku is the one to prototype against** — cheapest and
quickest, and enough for a corpus question that needs a search and a fetch. Move up when
the questions get harder, not before.

**The ids are complete as written.** Do not append a date to them; a dated snapshot id is
a different thing, and Foundry deployment names are different again — which is why the
host matters as much as the key.

### 4. Ask something

The same question as above, against the same corpus, on the other protocol:

```
$ intel ask "Which failure modes does the corpus identify for LLM wikis at scale?"

  ▸ wiki_search query=LLM wiki failure modes at scale limit=10
    ✓ design:llm-wiki-failure-modes-at-scale — …
  ▸ wiki_get key=llm-wiki-failure-modes-at-scale
    ✓ …

<the answer, citing design:llm-wiki-failure-modes-at-scale>
```

Nothing above the runtime changes between the two: the same sources, the same tools, the
same citations. That is the point of the seam — a deployment moves from one provider to
another by changing two lines of config.

### If it goes wrong

| what you see | what it means |
|---|---|
| `no host is set` | no row has been chosen, and there is no default. Set `host: anthropic` |
| `set ANTHROPIC_FOUNDRY_API_KEY, …` | the host is `foundry-anthropic`, which is the other Messages row. Set `host: anthropic` |
| a 401 | the key was rejected. The remedy names the variable, never the value |
| a 404 | the model id — check it against Anthropic's published list |
| a 429 | Anthropic is rate limiting the account. Wait, or raise the account's limit |

---

## Azure AI Foundry

**What it is for.** Claude models served from your own Azure resource — inside your
subscription, inside your network boundary, billed through Azure. This is the enterprise
path, and the deployment this page was written for.

**One resource, two surfaces.** A Foundry resource answers both the Anthropic Messages
protocol and an OpenAI-compatible one, so Foundry is **two rows** — `foundry-anthropic`
and `foundry-openai`. They read different variables and are dialled at different paths,
which is why one `foundry` would have to be read twice to know which it meant. Which you
choose decides the variable names and nothing else — the resource, the deployments and
the key are the same either way.

### 1. Set two variables

Over the Anthropic protocol, which is the `foundry-anthropic` row:

```bash
# ~/.config/latent-intel/.env
ANTHROPIC_FOUNDRY_API_KEY=…
ANTHROPIC_FOUNDRY_RESOURCE=<your-resource-name>
```

Over the OpenAI-compatible surface, which is the `foundry-openai` row:

```bash
FOUNDRY_API_KEY=…
FOUNDRY_RESOURCE=<your-resource-name>
```

**One resource has one key**, so the second pair is usually unnecessary. A machine
already reaching Foundry over the Anthropic protocol reaches the OpenAI one with nothing
added: `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_RESOURCE` stand in wherever the
`FOUNDRY_*` pair is unset, and the reason names both, so nobody has to know the mapping
to act on it.

`ANTHROPIC_FOUNDRY_BASE_URL` does **not** stand in for `FOUNDRY_BASE_URL`. It addresses
the other surface of the same resource, and borrowing it would point a request at the
wrong protocol.

**A resource name or a full address, never both.** `…_RESOURCE` is the short way and
`…_BASE_URL` the explicit one; each implies the other. Setting both is reported rather
than ranked silently — they are two ways of saying one thing, so one of them is not doing
what whoever set it thinks, and the Anthropic SDK refuses the pair outright.

The two surfaces have different addresses, which is what a base URL has to get right:

```
https://<resource-name>.services.ai.azure.com/anthropic      the Messages protocol
https://<resource-name>.services.ai.azure.com/openai/v1      the OpenAI-compatible one
```

Azure's portal shows a target URI of `…/anthropic/v1/messages` on the deployment's
**Details** tab; the base URL is that without `/v1/messages`. A wrong address here reads
as a 404, not as an authentication failure.

### 2. Check it

Before anything is set, both rows say what they want — and the second row shows the
fallback in the same breath:

```
$ intel hosts

custom ← configured
  ✓ anthropic          ← in use  ANTHROPIC_API_KEY
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  · openrouter         set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
```

> The blocks from here down are written from the host declarations rather than captured
> from a live run — this deployment has no Foundry credentials yet. Everything above is
> captured.

### 3. Choose it

The Anthropic protocol, which is the `foundry-anthropic` row:

```
latent › /runtime custom
latent › /host foundry-anthropic
latent › /model claude-sonnet-5
```

```yaml
runtime: custom
runtimes:
  custom:
    host: foundry-anthropic
    model: claude-sonnet-5
```

The OpenAI-compatible surface, for the same resource — the same runtime, a different row:

```yaml
runtime: custom
runtimes:
  custom:
    host: foundry-openai
    model: claude-sonnet-5
```

**A model id here is a deployment name.** Azure's own wording: the deployment defaults to
the model's name, you may change it before deploying, and *"during inference, use the
deployment name in the `model` parameter"* — so it can differ from the model id. In
practice it is usually the model family, such as `claude-sonnet-5` or `claude-haiku-4-5`.

A dated string like `claude-sonnet-5-20260101` therefore only works if a deployment is
actually called that, which is why an id that succeeds against the vendor's own API can
404 here. The host matters as much as the key.

**Some models are Entra ID only.** Azure documents Claude Mythos 5-1, Mythos 5 and Mythos
Preview as supporting Microsoft Entra ID authentication and not API keys. This host reads
an API key, so those deployments are not reachable through it today.

### 4. Ask something

Nothing above the runtime changes: the same sources, the same tools, the same citations.
Moving a deployment from the vendor's API to your own Azure resource is two lines of
config and a different pair of variables.

```
$ intel doctor

runtimes
  custom loop
    ✓ custom ← configured
  sdk runner
    · openai-agents — set OPENAI_API_KEY for host 'openai'
    ✓ sdk-anthropic
  delegated
    ✓ claude-cli
  model: claude-sonnet-5
```

### If it goes wrong

The first three come from this program; the rest are Azure's, and the causes below are
the ones Microsoft documents for Claude on Foundry.

| what you see | what it means |
|---|---|
| `set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or …` | nothing is set yet. The key, then either the resource name or the full address |
| `set only one of … — both are set` | a resource name and a base URL are both exported. Unset whichever you did not mean |
| `… is set but empty` | a variable exported with nothing in it. This program reads that as unset and the SDK reads it as set, so it is named before anything else |
| 401 Unauthorized | the key is wrong or expired. The remedy names the variable, never the value |
| 403 Forbidden | permissions, not credentials — the identity needs **Contributor** or **Owner** on the resource group |
| 404 Not Found | the endpoint URL **or** the deployment name. Check the base URL shape first, then that a deployment by that name exists on the resource |
| 429 Too Many Requests | the rate limit for your **subscription tier**, not one deployment. Back off and retry; a persistent ceiling needs a quota increase request |
| 400 `invalid_request_error` naming data retention | the model is a Covered Model that requires data retention and the subscription has zero data retention enabled. Not fixable from here — it is settled with Anthropic, or on a different subscription |

---


## OpenRouter

**What it is for.** One key reaches models from every vendor, and a handful of them are
free. If you have no endpoint of your own, start here — it is the cheapest way to see the
whole thing work, and nothing you learn is wasted when you move to a paid endpoint later.

### 1. Set one variable

```bash
# ~/.config/latent-intel/.env        machine-wide
OPENROUTER_API_KEY=sk-or-…
```

A `.env` beside a project file does the same thing for one deployment, and anything
already exported beats both. Get the key from
[openrouter.ai/keys](https://openrouter.ai/keys).

### 2. Check it

```
$ intel hosts

custom ← configured
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  ✓ openrouter         ← in use  OPENROUTER_API_KEY
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
```

### 3. Choose it

Three commands in the shell. Each persists immediately, the way `/connect` does, and each
reports what is still missing — so the next thing to fix is always on screen:

```
latent › /runtime custom
runtime custom
! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
latent › /host openrouter
host openrouter for custom
! no model is set — set `model:` under `runtimes: custom:`, because every host names its
models differently and there is no default
latent › /model poolside/laguna-s-2.1:free
model poolside/laguna-s-2.1:free for custom
```

The last line prints no warning, which is how you know nothing is left. The same three
values in config, if you would rather write the file:

```yaml
runtime: custom
runtimes:
  custom:
    host: openrouter
    model: poolside/laguna-s-2.1:free
```

**Why one runtime?** A runtime is who owns the loop; the *protocol* is a property of the
endpoint, and the host row names it. OpenRouter speaks the OpenAI-compatible one, so
`host: openrouter` under `runtime: custom` is right whatever vendor the model you pick
comes from — `anthropic/claude-sonnet-4.5` included.

`openai-agents` is the SDK-run sibling for the chat rows: the same protocol, the same
hosts and the same variables, so everything in this section applies to it unchanged once
the `agents` extra is installed.

**Model ids are `<vendor>/<model>`**, listed at
[openrouter.ai/models](https://openrouter.ai/models). A `:free` suffix is a free tier of
that model — rate-limited, but real. `poolside/laguna-s-2.1:free` is the one used
throughout this section.

### 4. Ask something

`intel doctor` should now mark the runtime configured, with the model beside it:

```
runtimes
  custom loop
    ✓ custom ← configured
  sdk runner
    · openai-agents — set OPENAI_API_KEY for host 'openai'
    · sdk-anthropic — set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  delegated
    ✓ claude-cli
  model: poolside/laguna-s-2.1:free
```

Then ask something only your sources can answer. The agent reaches them through tools
rather than being handed the text, and you see each call as it happens:

```
$ intel ask "What does the corpus conclude about compiled knowledge versus retrieval,
             and what does it say the tradeoff is?"

  ▸ wiki_search query=compiled knowledge vs retrieval limit=10
    ✓ design:compiled-knowledge-vs-retrieval — …
    ✓ design:graph-structure-vs-plain-retrieval — …
  ▸ wiki_get key=compiled-knowledge-vs-retrieval
    ✓ …

<the answer, citing design:compiled-knowledge-vs-retrieval and the pages beside it>
```

**Ask the corpus something the corpus knows.** A general question — "what is a context
store?" — is answered from the model's own training, and tells you nothing about whether
your sources are reachable. A question whose answer lives in an attached page exercises
search, fetch and citation in one go, and a wrong answer is visible immediately.

**When the sources fall short, the agent says so** rather than filling the gap quietly:

```
I cannot find a definition for "context store" in the available documentation from the
attached wikis.
```

That is instructed, not accidental — the agent is told to cite what it uses and to say
when the sources do not answer. Whether it should then answer anyway from general
knowledge is a posture a deployment sets, not a property of the engine.

### If it goes wrong

| what you see | what it means |
|---|---|
| `set OPENROUTER_API_KEY for host 'openrouter'` | the variable is unset, or the `.env` was not loaded — `intel doctor` says which |
| a 401 | the key was rejected. The remedy names the variable, never the value |
| a 404 | the model id. Check it against openrouter.ai/models — `<vendor>/<model>`, and `:free` is part of the id |
| a 429 | the model's shared free pool is busy — see below. Not your key |
| `no model is set` | a host-level reason this is not: no host has a default model, because every one of them names models differently |

### Free models

An id ending in `:free` costs nothing. It is served from a pool shared by every free user
of that model, so a `429` is about the model being busy, not about your key, your account
or the size of your question — retry, or pick another id.

All of these accept tool calls, which is what an agent needs to reach your sources:

| model | context | good for |
|---|---|---|
| `poolside/laguna-s-2.1:free` | 262,144 | **start here** — a good all-round default |
| `poolside/laguna-xs-2.1:free` | 262,144 | the same, smaller and quicker |
| `nvidia/nemotron-3.5-lightning:free` | 1,000,000 | fast, and the largest context on the list |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 1,000,000 | the most capable, and the slowest to answer |
| `dots-studio/dots-3-note-preview:free` | 512,000 | long documents |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262,144 | capable, quicker than Ultra |
| `nex-agi/nex-n2.5-pro:free` | 262,144 | general use |
| `nex-agi/nex-n2.5-mini:free` | 262,144 | general use, smaller and quicker |
| `inclusionai/ling-3.0-flash-vl:free` | 262,144 | images as well as text |
| `inclusionai/ling-3.0-flash-fin:free` | 262,144 | tuned for finance |
| `inclusionai/ling-3.0-flash-sante:free` | 262,144 | tuned for health |
| `cohere/north-mini-code:free` | 256,000 | code |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 256,000 | step-by-step reasoning |
| `liquid/lfm-2.5-2.6b:free` | 65,536 | small and quick; short questions |

**Smaller is not always better.** A small model given tools will sometimes search the same
thing repeatedly, or call for a document by the wrong key, and give up before it answers.
If a question that should work comes back empty, try one of the larger rows before
assuming the source is at fault.

**One free model is not usable here.** `nvidia/nemotron-3.5-content-safety:free` accepts no
tool calls, so an agent configured with it reaches none of your sources.

**Two are restricted.** `thinkingmachines/inkling:free` and
`thinkingmachines/inkling-small:free` return `403` — OpenRouter serves them only to
applications on its published list at [openrouter.ai/apps](https://openrouter.ai/apps),
which this client is not on.

Free ids change often. The current list is at
[openrouter.ai/models](https://openrouter.ai/models), filtered by price.

---



Every variable on this page is named, never printed. `intel hosts` and `intel doctor` are
both safe to paste into a support thread.

---

## A local server

**What it is for.** Prototyping that costs nothing and sends nothing anywhere. The model
runs on your own machine, so there is no key, no quota, no shared pool, and no question
about where a client's corpus went. It is the right place to shake out a deployment
before pointing it at a paid endpoint.

[Ollama](https://ollama.com) is used below; vLLM and LM Studio serve the same
OpenAI-compatible API and configure identically.

### 1. Set one variable

```bash
# ~/.config/latent-intel/.env
LOCAL_OPENAI_BASE_URL=http://localhost:11434/v1
```

The address is the whole configuration — nothing can guess where your server listens.
`LOCAL_OPENAI_API_KEY` is optional and almost never needed: a placeholder is sent when it
is unset, because the SDK refuses to build a client with no credential at all, and a
server on your own machine authenticates nobody. It exists for the one that does.

These are the host's own variables and not the `openai` row's, deliberately: a key issued
for the public API must never be forwarded to whatever is listening on the LAN.

### 2. Check it

```
$ intel hosts

custom ← configured
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  · openrouter         set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  ✓ local              ← in use  LOCAL_OPENAI_BASE_URL
```

### 3. Choose it

The runtime is `custom` and the row is `local` — the row carries the protocol, so
Ollama's OpenAI-compatible API changes nothing else. The SDK-run sibling for this row,
`openai-agents`, reaches it on the same variables, if you want the framework's loop
against a server that costs nothing.

```
latent › /runtime custom
latent › /host local
latent › /model qwen3:14b
```

```yaml
runtime: custom
runtimes:
  custom:
    host: local
    model: qwen3:14b
```

**The model must support tool calls**, or the agent reaches none of your sources. Ollama
says so directly:

```
$ ollama list                      # what you have pulled
$ ollama show qwen3:14b

  Capabilities
    completion
    tools          ← this one
    thinking
```

### 4. Ask something

```
$ intel ask "What do the notes say about context collapse?"

  ▸ files_search query=context collapse limit=5
    ✓ notes:collapse.md — Context collapse Asking a model to rewrite an accumulated…

The notes mention "context collapse" in the file `notes:collapse.md`, which states:
"**Context collapse** — Asking a model to rewrite an accumulated context end to end
loses detail."

This appears to describe a scenario where summarizing or rewriting a lengthy context
risks omitting important details.
```

### Choosing a local model

**Declared tool support is not the same as using it.** Measured on one laptop, same
question, same corpus:

| model | size | declares `tools` |
|---|---|---|
| `qwen3:14b` | 9.3 GB | yes |
| `deepseek-r1:1.5b` | 1.1 GB | yes |

Both declare it; only the larger one used it. The smaller answered far quicker and
invented its answer outright, with nothing in the output to say so — no tool call above
it, and a confident tone.

Treat a local model's speed as the tradeoff it is: pick the largest one your machine runs
at a tolerable pace, and check its answers against the source until you trust it.

### If it goes wrong

| what you see | what it means |
|---|---|
| `set LOCAL_OPENAI_BASE_URL for host 'local'` | the variable is unset — the address is the one thing with no default |
| `could not reach http://localhost:11434/v1` | the server is not running. Start it (`ollama serve`), or check the port |
| a 404 | the model is not pulled on this machine — `ollama pull <model>`, and `ollama list` shows what is there |
| a confident answer with no tool call above it | the model is ignoring the tools. Check `ollama show` for the `tools` capability, and prefer a larger model |
