# Demos

Scripted walkthroughs — one task each, start to finish, with **captured output rather
than illustrations**. They live beside the code that has to keep producing that output,
so a demo that has gone stale is a demo that fails review.

| Demo | Shows | Needs |
|---|---|---|
| [choosing-a-host.md](choosing-a-host.md) | finding every model endpoint this machine could reach, and what each one costs to set up | nothing until the last step |

Each one ends with a **Reproducing this** section, and each runs under
`LATENT_INTEL_HOME=/tmp/demo` so following one disturbs nothing you have configured.

Writing a new one: capture at `COLUMNS=100`, say plainly where a block is illustrative
rather than captured, and keep any client name, bucket or store id out of it — the same
rule `src/` is held to, and `tests/test_projects_acceptance.py` greps for.
