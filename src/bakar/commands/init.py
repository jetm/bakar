"""bakar init subcommand - interactive workspace creation wizard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from bakar.workspace_config import write_workspace_config

if TYPE_CHECKING:
    from pathlib import Path


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
