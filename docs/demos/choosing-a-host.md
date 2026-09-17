# Demo — choosing a host

**Five minutes, no credentials required until the last step.** What it shows: a machine
that has been handed the engine and nothing else can find out, in one command, every
model endpoint it could reach and exactly what each one would cost it to set up.

Everything below is captured output, not an illustration, except where it says so.

---

## 1. What could this machine reach?

```
$ intel hosts

claude-cli — no hosts

custom
  ! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  · openrouter         set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'

openai-agents
  · openai          set OPENAI_API_KEY for host 'openai'
  · foundry-openai  set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  · openrouter      set OPENROUTER_API_KEY for host 'openrouter'
  · azure-openai    set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local           set LOCAL_OPENAI_BASE_URL for host 'local'

sdk-anthropic
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'

Names of variables, never values — safe to paste. Set them in a `.env` beside the
project file, or export them.
```

Seven endpoints across two protocols, and the shortest way in is visible without reading
any documentation: `openrouter` wants one variable, `azure-openai` wants three.

Five details in that output are the whole point of the design:

- **`claude-cli — no hosts`** is a statement, not an omission. That runtime shells to a
  binary that has already chosen its endpoint, so it has nothing to declare — and saying
  so beats leaving it out, which reads as "not installed".
- **`custom` sees all seven and the SDK runtimes see a subset.** The protocol is a
  property of the endpoint, carried on the row, so one loop reaches both — while a
  runtime built on one SDK can dial no other, and listing the rest would offer it
  endpoints it cannot reach.
- **`no host is set`** is printed once above the rows rather than against one of them.
  Seven rows across two protocols make any default right for at most one deployment, so
  there is none.
- **`(or ANTHROPIC_FOUNDRY_API_KEY)`** is a fallback: a machine already reaching Foundry
  over the Anthropic protocol needs nothing more to reach it over the OpenAI one. Nobody
  has to know that mapping — the line names it.
- **`AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN`** is a choice, not a list. A resource
  with key authentication disabled cannot issue the first, and Entra ID is the way in.

No value is ever printed, so the whole block is safe to paste into a support thread.

---

## 2. Set one variable

```
$ export OPENROUTER_API_KEY=…
$ intel hosts

custom
  ! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  · foundry-anthropic  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  · openai             set OPENAI_API_KEY for host 'openai'
  · foundry-openai     set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry-openai'
  ✓ openrouter         OPENROUTER_API_KEY
  · azure-openai       set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  · local              set LOCAL_OPENAI_BASE_URL for host 'local'
```

The row it was asked for is the row that flipped. A host is ready when its own
declaration is satisfied and at no other time.

---

## 3. Choose it

In the shell (illustrative — the two commands persist immediately, the way `/connect`
does):

```
latent › /runtime custom
runtime custom
! no host is set — set `host:` under `runtimes: custom:`; `intel hosts` lists them
latent › /host openrouter
host openrouter for custom
! no model is set — set `model:` under `runtimes: custom:`, because every host names
  its models differently and there is no default
latent › /model anthropic/claude-sonnet-4.5
model anthropic/claude-sonnet-4.5 for custom
```

Each command reports the reason still standing after it, so the next thing to fix is
always on screen: the runtime was chosen while no host had been, the host moved to one
this machine can reach, and only the model was left.

That middle warning is worth pausing on. It is a **runtime-level** reason — an unset
model stops every host at once — so `intel hosts` prints it once above the table rather
than blaming a host for it:

```
$ intel hosts

custom ← configured
  ! no model is set — set `model:` under `runtimes: custom:`, because every host names its models
differently and there is no default
  · anthropic          set ANTHROPIC_API_KEY for host 'anthropic'
  …
  ✓ openrouter         ← in use  OPENROUTER_API_KEY
  …
```

---

## 4. Confirm

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

$ intel doctor

runtimes
  custom loop
    ✓ custom ← configured
  sdk runner
    · openai-agents — set OPENAI_API_KEY for host 'openai'
    · sdk-anthropic — set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry-anthropic'
  delegated
    ✓ claude-cli
  model: anthropic/claude-sonnet-4.5
```

`intel doctor` and `intel hosts` say the same thing about `openrouter` in the same
words, because it is the same function answering: `doctor` asks about the host that is
configured, `hosts` asks about all of them.

---

## What this demonstrates

A host is **a row, not a module**. Each row declares what its endpoint needs once, and
the reason printed above, the remedy attached to a 401 or a 404, and the arguments the
SDK client is built with all read that one declaration. A name cannot be reported missing
under one spelling and read under another, and the next endpoint — another cloud's
gateway, another resource, a server on the desk — arrives as a row rather than a module.

- Which variables each host wants, and why: [`../user-guide.md`](../user-guide.md#choosing-a-runtime)
- The rows themselves, and the rules they are read by: `src/latent_intel/agent/hosts.py`
  — one table, whichever runtime dials it

## Reproducing this

```bash
LATENT_INTEL_HOME=/tmp/demo intel hosts
```

`LATENT_INTEL_HOME` gives the walkthrough a throwaway config, so running it disturbs
nothing you have set up. Captured at `COLUMNS=100`.
