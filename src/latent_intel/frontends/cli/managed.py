"""`intel managed` — the agent `claude-managed-agents` runs, created ahead of any turn.

A managed agent is a versioned definition the service holds. `sync` builds it from the
active deployment and what is attached, and creates it or updates it to a new version;
what that returns is kept in your own config for the deployment, never in the
deployment's file. A turn refuses to run a version that no longer matches.
"""

from __future__ import annotations

import json

import anyio
import typer
from rich.markup import escape

from ... import config as config_module
from ... import settings as settings_module
from ...models import SessionError
from .._shared import console, err_console, report, session_scope

#: The runtime this command provisions. Named here because the command is its own.
RUNTIME = "claude-managed-agents"

app = typer.Typer(name="managed", help=__doc__, no_args_is_help=True)


@app.command()
def sync(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the agent it would create; make no request."
    ),
) -> None:
    """Create or update the managed agent for the active deployment."""
    anyio.run(lambda: _sync(dry_run))


async def _sync(dry_run: bool) -> None:
    async with session_scope() as (session, problems):
        report(problems)
        if problems and not dry_run:
            # An agent created without a source would run without it, and every turn
            # after would match it: a smaller deployment, called the declared one.
            err_console.print(
                "[fail]✗[/] a source did not attach, so the agent would be created "
                "without it [dim]— fix it, or `--dry-run` to see the rest[/]"
            )
            raise typer.Exit(1)
        try:
            done = await session.provision(RUNTIME, dry_run=dry_run)
        except SessionError as exc:
            err_console.print(f"[fail]✗[/] {escape(str(exc))}")
            raise typer.Exit(1) from exc

    if done.synced is None:
        print(json.dumps(done.definition, indent=2))
        return
    config = config_module.load()
    resolved = settings_module.load()
    deployment = resolved.project_name
    config.overlay(resolved.overlay_key).runtimes[RUNTIME] = {"synced": done.synced}
    config_module.save(config)
    console.print(
        f"[ok]✓[/] agent [source]{escape(str(done.synced['agent_id']))}[/] "
        f"[dim]version {escape(str(done.synced['version']))} for "
        f"{escape(deployment or 'no deployment')}[/]"
    )
