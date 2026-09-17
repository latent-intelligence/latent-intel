# Demo — choosing a host

**Five minutes, no credentials required until the last step.** What it shows: a machine
that has been handed the engine and nothing else can find out, in one command, every
model endpoint it could reach and exactly what each one would cost it to set up.

Everything below is captured output, not an illustration, except where it says so.

---

## 1. What could this machine reach?

```
$ intel hosts

anthropic
  ! foundry    ← configured  set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry'
  ! anthropic  set ANTHROPIC_API_KEY for host 'anthropic'

claude-cli — no hosts

openai
  ! openai        ← configured  set OPENAI_API_KEY for host 'openai'
  ! foundry       set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry'
  ! openrouter    set OPENROUTER_API_KEY for host 'openrouter'
  ! azure-openai  set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  ! local         set LOCAL_OPENAI_BASE_URL for host 'local'

Names of variables, never values — safe to paste. Set them in a `.env` beside the
project file, or export them.
```

Seven endpoints across two protocols, and the shortest way in is visible without reading
any documentation: `openrouter` wants one variable, `azure-openai` wants three.

Three details in that output are the whole point of the design:

- **`claude-cli — no hosts`** is a statement, not an omission. That runtime shells to a
  binary that has already chosen its endpoint, so it has nothing to declare — and saying
  so beats leaving it out, which reads as "not installed".
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

openai
  ! openai        ← configured  set OPENAI_API_KEY for host 'openai'
  ! foundry       set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry'
  ✓ openrouter
  ! azure-openai  set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  ! local         set LOCAL_OPENAI_BASE_URL for host 'local'
```

The row it was asked for is the row that flipped. A host is ready when its own
declaration is satisfied and at no other time.

---

## 3. Choose it

In the shell (illustrative — the two commands persist immediately, the way `/connect`
does):

```
latent › /runtime openai
runtime openai
! set OPENAI_API_KEY for host 'openai'
latent › /host openrouter
host openrouter for openai
! no model is set — set `model:` under `runtimes: openai:`, because every host names
  its models differently and there is no default
latent › /model anthropic/claude-sonnet-4.5
model anthropic/claude-sonnet-4.5 for openai
```

Each command reports the reason still standing after it, so the next thing to fix is
always on screen: the runtime was chosen while its default host was still `openai`, the
host moved to one this machine can reach, and only the model was left.

That middle warning is worth pausing on. It is a **runtime-level** reason — an unset
model stops every host at once — so `intel hosts` prints it once above the table rather
than blaming a host for it:

```
$ intel hosts

openai
  ! no model is set — set `model:` under `runtimes: openai:`, because every host names
its models differently and there is no default
  ✓ openai        ← configured
  …
```

---

## 4. Confirm

```
$ intel hosts

openai
  ! openai        set OPENAI_API_KEY for host 'openai'
  ! foundry       set FOUNDRY_API_KEY (or ANTHROPIC_FOUNDRY_API_KEY), FOUNDRY_RESOURCE (or
ANTHROPIC_FOUNDRY_RESOURCE) or FOUNDRY_BASE_URL for host 'foundry'
  ✓ openrouter    ← configured
  ! azure-openai  set AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN, AZURE_OPENAI_ENDPOINT,
OPENAI_API_VERSION for host 'azure-openai'
  ! local         set LOCAL_OPENAI_BASE_URL for host 'local'

$ intel doctor

runtimes
  custom loop
    · anthropic — set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry'
    ✓ openai ← configured
  sdk runner
    · sdk-anthropic — set ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE or
ANTHROPIC_FOUNDRY_BASE_URL for host 'foundry'
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
- The rows themselves: `src/latent_intel/agent/runtimes/{anthropic,openai}.py`
- The rules they are read by: `src/latent_intel/agent/hosts.py`

## Reproducing this

```bash
LATENT_INTEL_HOME=/tmp/demo intel hosts
```

`LATENT_INTEL_HOME` gives the walkthrough a throwaway config, so running it disturbs
nothing you have set up. Captured at `COLUMNS=100`.
