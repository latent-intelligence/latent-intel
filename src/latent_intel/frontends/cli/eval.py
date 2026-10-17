"""`intel eval` — batches of recorded runs, for `latent-eval` to score.

A thin wrapper over `batch`: the library plans and runs, this prints and asks. Scoring
happens outside, over the recordings. The shell has no `/eval run`; inside it, record
what you do by hand with `/record --purpose eval`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import typer
from rich.markup import escape

from ... import batch
from .._shared import console, err_console

app = typer.Typer(name="eval", help=__doc__, no_args_is_help=True)


@app.command("run")
def run(
    cases: Path = typer.Argument(..., help="A latent-eval dataset: .yaml or .jsonl."),
    variants: Path = typer.Argument(..., help="A YAML file with a `variants:` list."),
    repeat: int = typer.Option(1, "--repeat", help="Runs per case and variant."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the matrix and spend nothing."
    ),
    yes: bool = typer.Option(False, "--yes", help="Run paid variants without asking."),
) -> None:
    """Every case through every variant, `--repeat` times, each recorded."""
    try:
        planned = batch.plan(cases, variants, repeat=repeat)
    except batch.BatchError as exc:
        err_console.print(f"[fail]✗[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc

    for variant in planned.variants:
        cost = "[warn]paid[/]" if variant.paid else "[ok]free[/]"
        if variant.reason:
            cost += f"  [fail]{escape(variant.reason)}[/]"
        console.print(
            f"  [source]{escape(variant.id)}[/]  {cost}  "
            f"[dim]{escape(variant.deployment_name)} · {escape(variant.runtime)} · "
            f"{escape(variant.model or 'default model')} · "
            f"{escape(variant.host or 'no host')}[/]"
        )
    if not planned.cases_stamped:
        console.print(
            f"[warn]![/] [dim]{escape(planned.cases_name)} is unstamped — read as "
            "latent-eval.dataset v1, its fields unchecked[/]"
        )
    console.print(
        f"[dim]{len(planned.cases)} cases × {len(planned.variants)} variants × "
        f"{planned.repeat} = {planned.count} runs → {escape(str(planned.directory))}[/]"
    )
    if dry_run:
        return
    if blocked := [v.id for v in planned.variants if v.reason]:
        err_console.print(
            f"[fail]✗[/] {escape(', '.join(blocked))} cannot run here "
            "[dim]— see above[/]"
        )
        raise typer.Exit(1)

    paid = [v.id for v in planned.variants if v.paid]
    if paid and not yes:
        if not sys.stdin.isatty():
            err_console.print(
                "[fail]✗[/] paid variants and no one to ask [dim]— add --yes[/]"
            )
            raise typer.Exit(1)
        if not typer.confirm(f"Run {', '.join(paid)}, which are paid?"):
            raise typer.Exit(1)

    def progress(recording: batch.Recording) -> None:
        mark = "[ok]✓[/]" if recording.status == "ok" else "[fail]✗[/]"
        console.print(f"{mark} [dim]{escape(recording.path)}[/]")

    manifest = anyio.run(lambda: batch.execute(planned, progress=progress))
    failed = sum(r["status"] != "ok" for r in manifest["recordings"])
    console.print(
        f"[dim]{planned.count - failed} of {planned.count} ok → "
        f"{escape(str(planned.directory / 'manifest.json'))}[/]"
    )
    if failed:
        raise typer.Exit(1)
