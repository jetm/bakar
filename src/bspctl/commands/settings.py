"""bspctl settings subcommand - read and write ~/.config/bspctl/config.toml."""

from __future__ import annotations

from typing import Annotated

import typer

from bspctl.commands._app import app, console
from bspctl.user_config import (
    get_setting,
    list_settings,
    set_setting,
    unset_setting,
)

settings_app = typer.Typer(help="Read and write recognized bspctl settings.", no_args_is_help=True)

_UNSET_MARKER = "(unset)"


@settings_app.command("list")
def list(  # noqa: A001 - Typer command name shadows the builtin intentionally
) -> None:
    """Print every recognized setting key with its current value or an unset marker."""
    for key, value in list_settings().items():
        if value is None:
            console.print(f"{key} = {_UNSET_MARKER}")
        else:
            console.print(f"{key} = {value!r}")


@settings_app.command("get")
def get(
    key: Annotated[str, typer.Argument(help="Dotted setting key, e.g. defaults.nxp.machine")],
) -> None:
    """Print the current value of one recognized setting key."""
    try:
        value = get_setting(key)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    if value is None:
        console.print(_UNSET_MARKER)
    else:
        console.print(repr(value))


@settings_app.command("set")
def set(  # noqa: A001 - Typer command name shadows the builtin intentionally
    key: Annotated[str, typer.Argument(help="Dotted setting key, e.g. defaults.nxp.machine")],
    value: Annotated[str, typer.Argument(help="Value to store; bool keys accept true/false/1/0")],
) -> None:
    """Validate, coerce, and write a recognized setting key to the config file."""
    try:
        set_setting(key, value)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


@settings_app.command("unset")
def unset(
    key: Annotated[str, typer.Argument(help="Dotted setting key, e.g. defaults.nxp.machine")],
) -> None:
    """Remove a recognized setting key from the config file."""
    try:
        unset_setting(key)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


app.add_typer(settings_app, name="settings")
