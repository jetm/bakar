"""bakar presets subcommand - manage named build presets."""

from __future__ import annotations

import typer
from rich.markup import escape

from bakar.commands._app import app, console
from bakar.preset_config import load_presets

presets_app = typer.Typer(help="Manage named build presets.", no_args_is_help=True)


@presets_app.command("list")
def list_presets() -> None:
    """Print all named presets with their family."""
    presets = load_presets()
    if not presets:
        console.print("No presets defined.")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Family")
    for preset in presets:
        table.add_row(preset.name, preset.family)
    console.print(table)


@presets_app.command("show")
def show_preset(name: str = typer.Argument(..., help="Preset name to show.")) -> None:
    """Print the full details of a named preset."""
    presets = load_presets()
    match = next((p for p in presets if p.name == name), None)
    if match is None:
        console.print(f"Preset '[bold]{escape(name)}[/bold]' not found.")
        raise typer.Exit(1)

    console.print(f"[bold]Name:[/bold]    {match.name}")
    console.print(f"[bold]Family:[/bold]  {match.family}")
    if match.machine:
        console.print(f"[bold]Machine:[/bold] {match.machine}")
    if match.distro:
        console.print(f"[bold]Distro:[/bold]  {match.distro}")
    if match.image:
        console.print(f"[bold]Image:[/bold]   {match.image}")

    specs = match.resolve()
    if len(specs) == 1:
        spec = specs[0]
        if spec.manifest:
            console.print(f"[bold]Manifest:[/bold] {spec.manifest}")
            if spec.branch:
                console.print(f"[bold]Branch:[/bold]   {spec.branch}")
        if spec.kas_yaml:
            console.print(f"[bold]KAS YAML:[/bold] {spec.kas_yaml}")
    else:
        console.print(f"[bold]Releases:[/bold] {len(specs)}")
        for i, spec in enumerate(specs, 1):
            parts = []
            if spec.manifest:
                parts.append(f"manifest={spec.manifest}")
            if spec.branch:
                parts.append(f"branch={spec.branch}")
            if spec.kas_yaml:
                parts.append(f"kas_yaml={spec.kas_yaml}")
            console.print(f"  [{i}] {', '.join(parts)}")


app.add_typer(presets_app, name="presets")
