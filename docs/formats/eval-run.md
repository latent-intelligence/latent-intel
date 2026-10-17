# Eval runs — `latent-intel.eval-run` v1

`intel eval run` puts every case through every variant, `--repeat` times, and records each
run. A run's directory holds one [recording](recording.md) per case, variant and epoch, and a
`manifest.json` that names them. `latent-eval` reads the recordings and turns them into run
records; the manifest says which recordings belong to one run and which never finished.

| | |
|---|---|
| Format | `latent-intel.eval-run` |
| Version | `1` — `MANIFEST_VERSION` in `src/latent_intel/batch.py` |
| Schema | none yet; one is written when a reader needs it |

## Layout

```
<root>/runs/<run-id>/
  manifest.json
  <variant-id>/<case-id>.<epoch>.jsonl
```

`<root>` is the `root:` of the deployment the run was started from: the active one, or the
one `LATENT_INTEL_PROJECT` names. It may be a local path or a URI such as `s3://…`. A run
id is UTC time to the second plus six hex digits, `20261017T142233Z-3f9a1c`, so run
directories sort by when they started.

## Manifest

Written before the first run and rewritten after each one, so a batch cut short still says
what exists and what never ran.

| Field | Type | Meaning |
|---|---|---|
| `format` | `"latent-intel.eval-run"` | |
| `format_version` | `1` | |
| `run_id` | string | the directory's name |
| `latent_intel_version` | string | the package that wrote it |
| `deployment` | string | the starting deployment's name, whose root holds the run |
| `started`, `finished` | ISO 8601, UTC | `finished` is null until the last run ends |
| `cases` | object | `name` (the file's name, not its path), `sha256` of its bytes, `count` |
| `repeat` | integer ≥ 1 | epochs per case and variant |
| `variants` | list of `{id, deployment, runtime}` | the deployment by name; options are left out, because a `cwd` or a binary path is a location |
| `recordings` | list | `path` (relative to the manifest), `case_id`, `variant_id`, `epoch`, `status` |

`status` is `pending`, `ok` (the turn ended in `agent_completed`) or `failed`: an
`agent_failed`, a source that did not attach, or an error the run recorded before it
stopped. A failed run's recording still opens with its header. One that failed before
its session existed has a header of what was known: runtime, deployment, case, variant,
epoch and metadata.

The model and host that answered are in each recording's `run_context`, along with
`case_id`, `variant_id`, `epoch`, the case's `metadata` and `purpose: eval`.

## Inputs

**Cases** are `latent-eval.dataset` v1 (its `docs/formats/dataset.md`), YAML or JSON Lines.
Only `id`, `input.question` and `metadata` are read. `reference` is never read, because the
answer is the scorer's business.

- **A stamped file** is held to the spec, as `latent-eval` would hold it when it scores: its
  stamp, its keys, and each case's fields. Only `format_version`, or a `format` of
  `latent-eval.dataset`, stamps a file.
- **An unstamped file** is read as `latent-eval` reads it, and the command says it is
  unstamped. A missing `input` is empty, and YAML's `context:` stands in for `input:`.
- **In either kind,** a case with no text `input.question` refuses the batch before anything
  runs, and every such case is named.

Ids name files, so two that differ only in case are refused: on a case-insensitive
filesystem they would be one file.

**Variants** are YAML:

```yaml
variants:
  - id: haiku-plain                 # letters, digits, . _ - ; names a directory
    deployment: research            # a name, or a path relative to this file;
                                    #   the starting deployment when absent
    runtime: anthropic-sdk
    options: {model: claude-haiku-4-5, host: anthropic}
```

`options` are laid over the deployment's own options for that runtime. Approval comes from
the deployment's settings, never from a variant. A variant may not be called `manifest.json`.

A variant whose deployment reports a problem refuses the batch. A source skipped for an unset
variable would otherwise leave the variant smaller than declared. A variant whose runtime
cannot run on this machine still appears in `--dry-run`, with the reason; a real run refuses
it.

## Reading a run

- Refuse a missing or unknown `format_version`.
- Ignore fields you do not know. Adding an optional field does not change the version.
- A `pending` entry has no file, or a file cut off by an interrupt.
- Read each recording as [`recording.md`](recording.md) says. Its header, not the
  manifest, is the record of what ran.
