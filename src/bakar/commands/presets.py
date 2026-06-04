"""bakar presets subcommand - manage named build presets."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import questionary
import typer
from rich.markup import escape

from bakar.commands._app import app, console
from bakar.preset_config import load_presets
from bakar.user_config import _dump_raw

_CONFIG_PATH = Path.home() / ".config" / "bakar" / "config.toml"

presets_app = typer.Typer(help="Manage named build presets.", no_args_is_help=True)


def _ask(question: questionary.Question) -> object:
    """Call question.ask() and abort cleanly on None (questionary swallows Ctrl-C)."""
    result = question.ask()
    if result is None:
        raise typer.Abort
    return result


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


def _is_tty() -> bool:
    """Return True when stdin is a TTY. Extracted for testability."""
    return sys.stdin.isatty()


@presets_app.command("add")
def add_preset() -> None:
    """Interactive wizard to add a new named preset to config.toml."""
    if not _is_tty():
        console.print("[red]bakar presets add requires an interactive terminal[/] - stdin is not a TTY.")
        raise typer.Exit(1)

    family: str = _ask(  # type: ignore[assignment]
        questionary.select(
            "BSP family:",
            choices=["nxp", "ti", "bbsetup", "generic"],
        )
    )

    name: str = _ask(  # type: ignore[assignment]
        questionary.text("Preset name (unique identifier):")
    )

    preset_dict: dict[str, object] = {"name": name, "family": family}

    if family in ("nxp", "ti"):
        preset_dict["manifest"] = _ask(questionary.text("Manifest filename (e.g. imx-6.6.52-2.2.2.xml):"))
        preset_dict["branch"] = _ask(questionary.text("Branch (e.g. lf-6.6.y):"))
        preset_dict["machine"] = _ask(questionary.text("Machine (e.g. imx8mpevk):"))
        preset_dict["distro"] = _ask(questionary.text("Distro (e.g. fsl-imx-xwayland):"))
        preset_dict["image"] = _ask(questionary.text("Image (e.g. imx-image-full):"))
    else:
        # generic / bbsetup
        preset_dict["kas_yaml"] = _ask(questionary.path("kas YAML path:", default="kas-generic.yml"))
        preset_dict["machine"] = _ask(questionary.text("Machine (e.g. qemux86-64):"))
        preset_dict["image"] = _ask(questionary.text("Image (e.g. avocado-os):"))

    config_path = _CONFIG_PATH
    if config_path.exists():
        with config_path.open("rb") as f:
            raw: dict[str, object] = tomllib.load(f)
    else:
        raw = {}

    presets_list: list[dict[str, object]] = raw.setdefault("presets", [])  # type: ignore[assignment]
    existing_names = {d.get("name") for d in presets_list}
    if name in existing_names:
        console.print(f"[red]Preset '[bold]{escape(name)}[/bold]' already exists in {config_path}.[/red]")
        raise typer.Exit(1)
    presets_list.append(preset_dict)

    _dump_raw(config_path, raw)
    console.print(f"[green]Preset '[bold]{escape(name)}[/bold]' added to {config_path}[/green]")


app.add_typer(presets_app, name="presets")
