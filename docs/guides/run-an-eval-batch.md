# Run an eval batch

Put a set of questions through several setups, each run recorded, for latent-eval to score. A
setup, a **variant**, is a deployment, a runtime and that runtime's options, so a batch compares
models, hosts, or one persona or skill set against another on the same questions. Scoring happens
in latent-eval, which reads the recordings; nothing here judges an answer.

## The cases

latent-eval's dataset format, `latent-eval.dataset` v1, as YAML or JSON Lines. Each case's
question is `input.question`; `id` and `metadata` are kept, and `reference` is never read.

```yaml
# cases.yaml
format: latent-eval.dataset
format_version: 1
name: collapse
cases:
  - id: cause
    input: {question: "What do the notes say causes context collapse?"}
  - id: remedy
    input: {question: "What remedy do the notes give for context collapse?"}
    metadata: {topic: remedies}
```

A file without the stamp is read as latent-eval reads one, and the batch says so. A case with no
question refuses the whole batch, naming every such case.

## The variants

```yaml
# variants.yaml
variants:
  - {id: sonnet, runtime: anthropic-sdk, options: {host: anthropic, model: claude-sonnet-5}}
  - {id: local, runtime: custom, options: {host: local, model: qwen3:14b}}
  - {id: terse, deployment: ./terse/terse.yaml, runtime: anthropic-sdk,
     options: {host: anthropic, model: claude-sonnet-5}}
```

`deployment` is a name, or a path relative to this file; without it, the active deployment.
`options` are laid over that runtime's options from the deployment and from your config, so a
model set in your config reaches every variant that sets none ([options](../runtimes.md#options)).
Approval is your config's `approval`, never a variant's. Each run is a fresh session on its
variant's deployment, with that deployment's sources attached.

## Plan, then run

```
$ intel eval run cases.yaml variants.yaml --repeat 2 --dry-run
  sonnet  paid  'anthropic-sdk' cannot run here — set ANTHROPIC_API_KEY for host 'anthropic'
research · anthropic-sdk · claude-sonnet-5 · anthropic
  local  free  'custom' cannot run here — set LOCAL_OPENAI_BASE_URL for host 'local'  research ·
custom · qwen3:14b · local
2 cases × 2 variants × 2 = 8 runs → ~/deployments/research/runs/20261025T165124Z-f2c561
```

Captured with the first two variants on a machine with neither host set up. The dry run reads
every file and builds every variant's runtime, and spends nothing. Every variant is paid except
one on the `local` host.

```bash
intel eval run cases.yaml variants.yaml --repeat 2          # asks before paid variants
intel eval run cases.yaml variants.yaml --repeat 2 --yes    # does not ask
```

Without a terminal to ask, paid variants need `--yes`. A variant that cannot run here refuses
the batch before anything runs, and so does a variant whose deployment reports a problem. A run
that fails is marked failed and the next one starts; the command exits 1 if any failed.

## What lands on disk

A batch needs an active deployment, and writes beneath its `root:`, which defaults to the
deployment file's directory:

```
<root>/runs/<run-id>/
├── manifest.json                   what ran, and each recording's status
├── sonnet/cause.1.jsonl            one recording per variant, case and repeat
├── sonnet/cause.2.jsonl
└── …
```

The manifest is written before the first run and after each one, so a batch cut short still says
what exists. The layout and fields: [`latent-intel.eval-run`](../contracts/eval-run.md). With no
active deployment the batch is refused, and so is the built-in `latent` one, since its directory
is inside the installed package.

## Known limits

- **The shell has no batch command.** Record by hand there with `/record <file> --purpose eval`.
- **A `claude-cli` variant counts as paid,** as every variant not on `local` does.
