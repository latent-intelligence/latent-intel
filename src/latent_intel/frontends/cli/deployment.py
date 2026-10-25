"""`intel deployment` — which deployment this install is serving.

One active deployment at a time. Several at once was rejected: it makes every source
ambiguous about which deployment it belongs to, and nobody has yet needed to answer one
question across two clients.

Nothing here edits a deployment file. The engine reads deployments and writes only the
user's own config — a deployment belongs to whoever owns it, and a tool that edited it
would make a checked-out deployment repo dirty on first use. `new` writes a deployment
that does not exist yet, and refuses one that does.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import typer
from rich.markup import escape

from ... import config as config_module
from ... import project as project_module
from ... import settings as settings_module
from .._shared import console, err_console

app = typer.Typer(name="deployment", help=__doc__, no_args_is_help=True)


@app.command("list")
def list_deployments() -> None:
    """Every deployment this install can find, and where each came from."""
    config = config_module.load()
    found, collisions = project_module.discover(
        config.deployment_paths, config_module.home()
    )
    active = settings_module.load().chosen

    if not found:
        console.print("[dim]no deployments found[/]")
        return
    for name, path in sorted(found.items()):
        mark = "[ok]✓[/]" if name == active else " "
        console.print(f"  {mark} [source]{name}[/]  [dim]{escape(str(path))}[/]")
    for note in collisions:
        err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")


@app.command()
def use(name: str = typer.Argument(..., help="A deployment name, or `none`.")) -> None:
    """Switch the active deployment."""
    config = config_module.load()
    if name == "none":
        config.deployment = None
        config_module.save(config)
        console.print("[dim]no deployment[/]")
        return

    found, _ = project_module.discover(config.deployment_paths, config_module.home())
    if name not in found:
        known = ", ".join(sorted(found)) or "none found"
        err_console.print(
            f"[fail]✗[/] no deployment '{escape(name)}' "
            f"[dim](found: {escape(known)})[/]"
        )
        raise typer.Exit(1)

    config.deployment = name
    config_module.save(config)
    resolved = settings_module.load()
    console.print(f"[dim]deployment[/] [source]{escape(name)}[/]")
    for note in resolved.problems:
        err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")


@app.command()
def show() -> None:
    """Every resolved value, and which layer it came from.

    The question a layered configuration invites is "why is this here?", and this is the
    answer. Without it, layering is a guessing game with extra steps.
    """
    resolved = settings_module.load()
    if resolved.project is None:
        console.print("[dim]no deployment — engine defaults only[/]")
    else:
        loaded = resolved.project
        console.print(
            f"[accent.strong]{escape(loaded.title)}[/] [dim]({loaded.name})[/]"
        )
        console.print(f"[dim]{escape(str(loaded.path))}[/]")
        if loaded.description:
            console.print(f"[dim]{escape(loaded.description)}[/]")
        root = str(loaded.root) if loaded.root is not None else "unresolved"
        console.print(f"[dim]{'root':<10}[/] {escape(root)}")

    console.print("\n[accent.strong]sources[/]")
    for spec in resolved.sources:
        layer = resolved.origin.get(f"source:{spec.id}", "?")
        where = spec.from_registry or spec.target or ""
        console.print(
            f"  [source]{spec.id:<16}[/] [kind]{spec.kind:<7}[/] "
            f"[dim]{escape(where)}[/] [dim]({layer})[/]"
        )
    if not resolved.sources:
        console.print("  [dim]none[/]")

    console.print("\n[accent.strong]settings[/]")
    for key in ("runtime", "approval", "web"):
        value = getattr(resolved, key) or "—"
        console.print(
            f"  [dim]{key:<10}[/] {escape(str(value))} "
            f"[dim]({resolved.origin.get(key, 'engine')})[/]"
        )
    if resolved.registry_path:
        console.print(f"  [dim]{'registry':<10}[/] {escape(resolved.registry_path)}")

    for note in resolved.problems:
        err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")


@app.command()
def new(
    name: str = typer.Argument(..., help="Lowercase letters, digits and hyphens."),
    directory: Path = typer.Argument(
        Path("."), help="Where its folder goes; the current directory by default."
    ),
) -> None:
    """Write a new deployment, `<directory>/<name>/<name>.yaml`, from the example.

    The one command that writes a deployment file, and only a new one: it refuses a
    folder that exists and a name already found, so it never shadows or edits one.
    """
    config = config_module.load()
    found, _ = project_module.discover(config.deployment_paths, config_module.home())
    if name in found:
        err_console.print(
            f"[fail]✗[/] a deployment '{escape(name)}' already exists: "
            f"[dim]{escape(str(found[name]))}[/]"
        )
        raise typer.Exit(1)
    try:
        path = project_module.create(directory, name)
    except (project_module.ProjectError, OSError) as exc:
        err_console.print(f"[fail]✗[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc

    folder = shlex.quote(str(path.parent.resolve()))
    console.print(f"[ok]✓[/] [dim]wrote[/] {escape(str(path.resolve()))}")
    console.print(f"[dim]next:[/] intel deployment add {escape(folder)}")
    console.print(f"[dim]then:[/] intel deployment use {escape(name)}")


@app.command()
def add(
    path: Path = typer.Argument(..., help="A deployment file, or a directory."),
) -> None:
    """Search a directory for deployments, without copying anything.

    A client repo checked out anywhere becomes usable in place, which is the actual
    "stand up deployment N+1" workflow — not copying a file into a hidden directory.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        err_console.print(f"[fail]✗[/] {escape(str(resolved))} does not exist")
        raise typer.Exit(1)

    directory = resolved if resolved.is_dir() else resolved.parent
    config = config_module.load()
    if str(directory) in config.deployment_paths:
        console.print(f"[dim]already searched[/] [dim]{escape(str(directory))}[/]")
        return
    # Refused rather than recorded: a search path that finds nothing reported as `✓`
    # sent people to `deployment use`, to be told the name did not exist.
    names = sorted({p.stem for p in project_module.candidates(directory)})
    if not names:
        err_console.print(
            f"[fail]✗[/] no deployment in {escape(str(directory))} "
            "[dim]— looked for NAME.yaml there and NAME/NAME.yaml one folder down; "
            "nothing recorded[/]"
        )
        raise typer.Exit(1)
    config.deployment_paths.append(str(directory))
    config_module.save(config)
    # What this directory now answers for: a name an earlier path holds stays there.
    found, problems = project_module.discover(
        config.deployment_paths, config_module.home()
    )
    here = sorted(n for n, p in found.items() if p.is_relative_to(directory))
    console.print(
        f"[ok]✓[/] [dim]searching[/] {escape(str(directory))} "
        f"[dim]({escape(', '.join(here) or 'every name shadowed')})[/]"
    )
    for note in problems:
        if f" {directory}{os.sep}" in note:
            err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")


@app.command()
def validate(
    name: str = typer.Argument(
        None, help="A deployment name; defaults to the active one."
    ),
) -> None:
    """Read a deployment and report what is wrong with it, without switching to it.

    Its sources are checked as far as they can be without attaching one: a local
    directory that is not there, an `mcp` command not on PATH. No server is started.
    """
    config = config_module.load()
    target = name or settings_module.load().chosen
    if not target:
        err_console.print("[fail]✗[/] no deployment to validate")
        raise typer.Exit(1)

    found, _ = project_module.discover(config.deployment_paths, config_module.home())
    if target not in found:
        err_console.print(f"[fail]✗[/] no deployment '{escape(target)}'")
        raise typer.Exit(1)

    try:
        loaded = project_module.load(found[target])
    except project_module.ProjectError as exc:
        err_console.print(f"[fail]✗[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc

    console.print(f"[dim]{escape(str(loaded.path))}[/]")
    console.print(
        f"[dim]sources[/] {len(loaded.sources)}  "
        f"[dim]commands[/] {'yes' if loaded.commands_dir else 'no'}  "
        f"[dim]skills[/] {'yes' if loaded.skills_dir else 'no'}"
    )
    problems = loaded.problems + project_module.check_sources(loaded)
    for note in problems:
        err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")
    if problems:
        raise typer.Exit(1)
    console.print("[ok]✓[/] [dim]valid[/]")
