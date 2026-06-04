"""bakar presets subcommand - manage named build presets."""

from __future__ import annotations

import typer

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


app.add_typer(presets_app, name="presets")
