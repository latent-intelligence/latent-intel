# latent-intel docs

New here? Read the [overview](overview.md), then [getting started](getting-started.md).

## Guides

In reading order:

1. [Connect sources](guides/connect-sources.md): folders, codebases and MCP servers, local or
   remote; by name from a registry; detaching.
2. [Use the shell](guides/use-the-shell.md): what is typed where, the slash commands, a
   deployment's own commands.
3. [Set up a deployment](guides/set-up-a-deployment.md): write one, make it active, check it;
   locations, saved commands, branding.
4. [Shape the agent](guides/shape-the-agent.md): persona, skills and subagents.
5. [Choose a runtime](guides/choose-a-runtime.md): pick a runtime and host, set it up, check it
   without spending.
6. [Ask with web access](guides/ask-with-web-access.md): the web scope, domain lists, and which
   runtimes honour them.
7. [Script and record](guides/script-and-record.md): `--json`, recordings, replay.
8. [Run an eval batch](guides/run-an-eval-batch.md): cases through variants, recorded for
   latent-eval.
9. [Use managed agents](guides/use-managed-agents.md): `intel managed sync`, staleness, cost.

## Reference

- [CLI](cli.md): every command, the options that matter, exit codes, environment variables.
  `--help` lists every option.
- [Python library](library.md): the public API by import path.
- [Deployment declaration](deployments.md): every key of a deployment and of your own config,
  credentials, and where state lives.
- [Runtimes and hosts](runtimes.md): every runtime and host, what each needs, options, ceilings,
  the web.

## Contracts

For tools that feed this package, or read what it writes:

- [What it reads and writes](contracts/index.md)
- [`latent-intel.recording` v1](contracts/recording.md): a session's events, headed by what
  produced them
- [`latent-intel.eval-run` v1](contracts/eval-run.md): a batch run's manifest and recordings

## Engineering

- [Contributing](../CONTRIBUTING.md): setup and checks.
- How it works, and what must not change: [`CLAUDE.md`](../CLAUDE.md).
- Design notes and measurements: to write, because the design records are still private.
