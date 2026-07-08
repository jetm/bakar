"""bakar init subcommand - interactive workspace creation wizard."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Literal

import questionary
import typer

from bakar.bsp_model import get_model
from bakar.commands._app import app, console
from bakar.workspace_config import write_workspace_config

_FAMILIES = ["nxp", "ti", "bbsetup", "generic"]


def _ask(question: questionary.Question) -> object:
    """Call question.ask() and abort cleanly on None (questionary swallows Ctrl-C)."""
    result = question.ask()
    if result is None:
        raise typer.Abort
    return result


def _scaffold_workspace(
    path: Path,
    family: Literal["nxp", "ti", "bbsetup", "generic"],
    settings: dict[str, str],
) -> None:
    """Scaffold a workspace directory layout and write ``.bakar.toml``.

    Pure function with no questionary interaction, so the four scaffold paths
    are unit-testable without mocking TTY prompts.

    - ``nxp`` / ``ti``: create the ``<path>/<family>/`` subdirectory, then
      write ``.bakar.toml`` with a ``[defaults.<family>]`` section.
    - ``bbsetup``: write a comment-only ``.bakar.toml`` marker (no
      ``[defaults]`` section) and create no subdirectories. ``bitbake-setup
      init`` drives its own interactive setup.
    - ``generic``: write ``.bakar.toml`` with a ``[defaults.generic]`` section
      and create no subdirectories.

    Raises ``FileExistsError`` when ``<path>/.bakar.toml`` already exists so the
    wizard can catch it and abort without clobbering an existing workspace.
    """
    marker = path / ".bakar.toml"
    if marker.exists():
        raise FileExistsError(str(marker))

    path.mkdir(parents=True, exist_ok=True)

    if family in ("nxp", "ti"):
        (path / family).mkdir(parents=True, exist_ok=True)
        write_workspace_config(path, family, settings)
    elif family == "bbsetup":
        marker.write_text("# bakar workspace root.\n")
    else:
        write_workspace_config(path, "generic", settings)


def _init_non_interactive(
    family: str,
    workspace: Path | None,
    manifest: str | None,
    machine: str | None,
    distro: str | None,
    image: str | None,
    kas_yaml: str | None,
) -> None:
    """Run non-interactive init: validate family, build settings, scaffold, report."""
    if family not in _FAMILIES:
        console.print(f"[red]unknown family:[/] {family!r} - must be one of {_FAMILIES}")
        raise typer.Exit(1)

    path = workspace or Path(".")

    settings: dict[str, str] = {}
    if family in ("nxp", "ti"):
        model = get_model(family)  # type: ignore[arg-type]
        settings["manifest"] = manifest or model.default_manifest or ""
        settings["machine"] = machine or model.default_machine or ""
        settings["distro"] = distro or model.default_distro or ""
        settings["image"] = image or model.default_image or ""
    elif family == "generic":
        settings["kas_yaml"] = kas_yaml or "kas-generic.yml"
        settings["machine"] = machine or "qemux86-64"
    # bbsetup: no settings needed.

    try:
        _scaffold_workspace(path, family, settings)  # type: ignore[arg-type]
    except FileExistsError as exc:
        console.print(f"[red]workspace already initialized:[/] {exc} already exists")
        raise typer.Exit(1) from exc

    console.print(f"[green]workspace scaffolded[/] at {path}")
    if family in ("nxp", "ti", "generic"):
        console.print(f"Next: [bold]bakar sync --workspace {path}[/]")


def _init_interactive(no_sync: bool, workspace: Path | None) -> None:
    """Run interactive init wizard: TTY check, questionary prompts, scaffold, optional sync."""
    if not sys.stdin.isatty():
        console.print(
            "[red]bakar init requires an interactive terminal[/] - stdin is not a TTY. "
            "Use --family to enable non-interactive mode."
        )
        raise typer.Exit(1)

    family_str: str = _ask(
        questionary.select(  # type: ignore[assignment]
            "BSP family:",
            choices=["nxp", "ti", "bbsetup", "generic"],
        )
    )

    workspace_str: str = _ask(
        questionary.path(  # type: ignore[assignment]
            "Workspace directory:",
            default=".",
        )
    )
    path = Path(workspace_str).expanduser()

    settings: dict[str, str] = {}
    if family_str in ("nxp", "ti"):
        model = get_model(family_str)  # type: ignore[arg-type]
        settings["manifest"] = _ask(
            questionary.text(  # type: ignore[assignment]
                "Manifest:",
                default=model.default_manifest,
            )
        )
        settings["machine"] = _ask(
            questionary.text(  # type: ignore[assignment]
                "Machine:",
                default=model.default_machine,
            )
        )
        settings["distro"] = _ask(
            questionary.text(  # type: ignore[assignment]
                "Distro:",
                default=model.default_distro,
            )
        )
        settings["image"] = _ask(
            questionary.text(  # type: ignore[assignment]
                "Image:",
                default=model.default_image,
            )
        )
    elif family_str == "generic":
        settings["kas_yaml"] = _ask(
            questionary.text(  # type: ignore[assignment]
                "kas YAML filename:",
                default="kas-generic.yml",
            )
        )
        settings["machine"] = _ask(
            questionary.text(  # type: ignore[assignment]
                "Machine:",
                default="qemux86-64",
            )
        )
    # bbsetup: no family-specific prompts.

    try:
        _scaffold_workspace(path, family_str, settings)  # type: ignore[arg-type]
    except FileExistsError as exc:
        console.print(f"[red]workspace already initialized:[/] {exc} already exists")
        raise typer.Exit(1) from exc

    console.print(f"[green]workspace scaffolded[/] at {path}")

    if no_sync:
        run_sync = False
    else:
        console.print("Downloading sources can take a while")
        run_sync = _ask(questionary.confirm("Run `bakar sync` now?", default=False))

    if run_sync:
        from bakar.commands.sync import sync

        sync(workspace=path)
        return

    if family_str in ("nxp", "ti", "generic"):
        console.print(f"Next: [bold]bakar sync --workspace {path}[/]")
    else:
        console.print(
            "Next: run [bold]bitbake-setup init[/] from inside the workspace to populate config/config-upstream.json"
        )


@app.command("init")
def init(
    family: Annotated[
        str | None,
        typer.Option("--family", "-f", help="BSP family (nxp/ti/bbsetup/generic); enables non-interactive mode"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory (default: current directory)"),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", help="Manifest filename (nxp/ti only)"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", help="Machine name"),
    ] = None,
    distro: Annotated[
        str | None,
        typer.Option("--distro", help="Distro (nxp/ti only)"),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Image (nxp/ti only)"),
    ] = None,
    kas_yaml: Annotated[
        str | None,
        typer.Option("--kas-yaml", help="KAS YAML filename (generic only)"),
    ] = None,
    no_sync: Annotated[
        bool,
        typer.Option("--no-sync", help="Skip sync after scaffolding (interactive mode only)"),
    ] = False,
) -> None:
    """Scaffold a new bakar workspace.

    Without ``--family``: interactive wizard using questionary prompts.
    Requires a TTY on stdin.

    With ``--family``: non-interactive mode - no TTY required. Settings are
    resolved from the provided flags, falling back to BSP model defaults for
    nxp/ti. Sync is never run in non-interactive mode.
    """
    if family is not None:
        _init_non_interactive(family, workspace, manifest, machine, distro, image, kas_yaml)
    else:
        _init_interactive(no_sync, workspace)
