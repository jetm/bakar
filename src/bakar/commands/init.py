"""bakar init subcommand - interactive workspace creation wizard."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import questionary
import typer

from bakar.bsp_model import get_model
from bakar.commands._app import app, console
from bakar.workspace_config import write_workspace_config


def _ask(question: questionary.Question) -> object:
    """Call question.ask() and abort cleanly on None (questionary swallows Ctrl-C)."""
    result = question.ask()
    if result is None:
        raise typer.Abort()
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

    if family in ("nxp", "ti"):
        (path / family).mkdir(parents=True, exist_ok=True)
        write_workspace_config(path, family, settings)
    elif family == "bbsetup":
        marker.write_text("# bakar workspace root.\n")
    else:
        write_workspace_config(path, "generic", settings)


@app.command("init")
def init() -> None:
    """Interactively scaffold a new bakar workspace.

    Walks through the BSP family, workspace directory, and family-specific
    defaults, writes ``.bakar.toml`` (and the family subdirectory for nxp/ti),
    then optionally kicks off ``bakar sync``.

    Requires an interactive terminal - questionary prompts cannot run without a
    TTY on stdin. Scriptable workspace creation is still available via
    ``mkdir <family>/ && touch .bakar.toml``.
    """
    if not sys.stdin.isatty():
        console.print(
            "[red]bakar init requires an interactive terminal[/] - stdin is not a TTY. "
            "Create the workspace manually with `mkdir <family>/ && touch .bakar.toml`."
        )
        raise typer.Exit(1)

    family: str = _ask(
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
    if family in ("nxp", "ti"):
        model = get_model(family)  # type: ignore[arg-type]
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
    elif family == "generic":
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
        _scaffold_workspace(path, family, settings)  # type: ignore[arg-type]
    except FileExistsError as exc:
        console.print(f"[red]workspace already initialized:[/] {exc} already exists")
        raise typer.Exit(1) from exc

    console.print(f"[green]workspace scaffolded[/] at {path}")

    console.print("Downloading sources can take a while")
    run_sync = _ask(questionary.confirm("Run `bakar sync` now?", default=False))

    if run_sync:
        from bakar.commands.sync import sync

        sync(workspace=path)
        return

    if family in ("nxp", "ti", "generic"):
        console.print(f"Next: [bold]bakar sync --workspace {path}[/]")
    else:
        console.print(
            "Next: run [bold]bitbake-setup init[/] from inside the workspace to populate config/config-upstream.json"
        )
