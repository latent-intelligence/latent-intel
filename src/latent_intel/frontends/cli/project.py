"""`intel project` — which deployment this install is serving.

One active project at a time. Several at once was rejected: it makes every source
ambiguous about which deployment it belongs to, and nobody has yet needed to answer one
question across two clients.

Nothing here writes a project file. The engine reads projects and writes only the user's
own config — a project belongs to whoever owns the deployment, and a tool that edited it
would make a checked-out project repo dirty on first use.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape

from ... import config as config_module
from ... import project as project_module
from ... import settings as settings_module
from .._shared import console, err_console

app = typer.Typer(name="project", help=__doc__, no_args_is_help=True)


@app.command("list")
def list_projects() -> None:
    """Every project this install can find, and where each came from."""
    config = config_module.load()
    found, collisions = project_module.discover(
        config.project_paths, config_module.home()
    )
    active = settings_module.load().project_name

    if not found:
        console.print("[dim]no projects found[/]")
        return
    for name, path in sorted(found.items()):
        mark = "[ok]✓[/]" if name == active else " "
        console.print(f"  {mark} [source]{name}[/]  [dim]{escape(str(path))}[/]")
    for note in collisions:
        err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")


@app.command()
def use(name: str = typer.Argument(..., help="A project name, or `none`.")) -> None:
    """Switch the active project."""
    config = config_module.load()
    if name == "none":
        config.project = None
        config_module.save(config)
        console.print("[dim]no project[/]")
        return

    found, _ = project_module.discover(config.project_paths, config_module.home())
    if name not in found:
        known = ", ".join(sorted(found)) or "none found"
        err_console.print(
            f"[fail]✗[/] no project '{escape(name)}' [dim](found: {escape(known)})[/]"
        )
        raise typer.Exit(1)

    config.project = name
    config_module.save(config)
    resolved = settings_module.load()
    console.print(f"[dim]project[/] [source]{escape(name)}[/]")
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
        console.print("[dim]no project — engine defaults only[/]")
    else:
        loaded = resolved.project
        console.print(
            f"[accent.strong]{escape(loaded.title)}[/] [dim]({loaded.name})[/]"
        )
        console.print(f"[dim]{escape(str(loaded.path))}[/]")
        if loaded.description:
            console.print(f"[dim]{escape(loaded.description)}[/]")

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
    for key in ("runtime", "approval"):
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
def add(
    path: Path = typer.Argument(..., help="A project file, or a directory."),
) -> None:
    """Search a directory for projects, without copying anything.

    A client repo checked out anywhere becomes usable in place, which is the actual
    "stand up project N+1" workflow — not copying a file into a hidden directory.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        err_console.print(f"[fail]✗[/] {escape(str(resolved))} does not exist")
        raise typer.Exit(1)

    directory = resolved if resolved.is_dir() else resolved.parent
    config = config_module.load()
    if str(directory) in config.project_paths:
        console.print(f"[dim]already searched[/] [dim]{escape(str(directory))}[/]")
        return
    config.project_paths.append(str(directory))
    config_module.save(config)

    found, _ = project_module.discover(config.project_paths, config_module.home())
    names = [n for n, p in found.items() if p.parent == directory]
    console.print(
        f"[ok]✓[/] [dim]searching[/] {escape(str(directory))} "
        f"[dim]({', '.join(sorted(names)) or 'nothing yet'})[/]"
    )


@app.command()
def validate(
    name: str = typer.Argument(
        None, help="A project name; defaults to the active one."
    ),
) -> None:
    """Read a project and report what is wrong with it, without switching to it."""
    config = config_module.load()
    target = name or settings_module.load().project_name
    if not target:
        err_console.print("[fail]✗[/] no project to validate")
        raise typer.Exit(1)

    found, _ = project_module.discover(config.project_paths, config_module.home())
    if target not in found:
        err_console.print(f"[fail]✗[/] no project '{escape(target)}'")
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
    for note in loaded.problems:
        err_console.print(f"[warn]![/] [dim]{escape(note)}[/]")
    if loaded.problems:
        raise typer.Exit(1)
    console.print("[ok]✓[/] [dim]valid[/]")


@app.command()
def migrate(
    to: str = typer.Option(None, "--to", help="Make a new project from these sources."),
) -> None:
    """Move a pre-projects `sources:` block out of the top of config.yaml.

    With `--to` this writes a real project file, so the migration is how someone gets
    their first project rather than a chore they are nagged about.
    """
    config = config_module.load()
    if not config.legacy_sources:
        console.print("[dim]nothing to migrate[/]")
        return

    backup = config_module.config_path().with_suffix(".yaml.bak")
    backup.write_text(
        config_module.config_path().read_text(encoding="utf-8"), encoding="utf-8"
    )

    moved = list(config.legacy_sources)
    config.legacy_sources = []

    if to:
        directory = settings_module.project_home()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{to}.yaml"
        rows = "\n".join(
            "  - {"
            + ", ".join(
                [f"id: {s.id}", f"kind: {s.kind}"]
                + ([f"from: {s.from_registry}"] if s.from_registry else [])
                + ([f"target: {s.target}"] if s.target else [])
                + (["remote: true"] if s.remote else [])
            )
            + "}"
            for s in moved
        )
        path.write_text(
            f"# {to} — migrated from config.yaml.\n\n"
            f"schema_version: 1\nname: {to}\ntitle: {to}\n\n"
            f"branding:\n  name: {to.upper()}\n\nsources:\n{rows}\n",
            encoding="utf-8",
        )
        config.project = to
        config_module.save(config)
        console.print(f"[ok]✓[/] [dim]wrote[/] {escape(str(path))}")
        console.print(f"[dim]project[/] [source]{escape(to)}[/]")
    else:
        for spec in moved:
            config.add(spec, None)
        config_module.save(config)
        console.print(f"[ok]✓[/] [dim]moved {len(moved)} source(s) to your overlay[/]")

    console.print(f"[dim]backup at {escape(str(backup))}[/]")
