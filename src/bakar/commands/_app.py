"""Typer app, Rich console, and startup state for all bakar subcommands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from bakar import __version__
from bakar.user_config import load_user_config
from bakar.vendor_config import load_vendors

if TYPE_CHECKING:
    from bakar.user_config import UserConfig

app = typer.Typer(
    help="BSP orchestrator (NXP i.MX + TI Sitara built-in).",
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_enable=False,
)
console = Console(stderr=True)

_VENDORS: list | None = None
_USER_CONFIG: UserConfig | None = None


def _get_vendors() -> list:
    global _VENDORS
    if _VENDORS is None:
        try:
            _VENDORS = load_vendors()
        except ValueError as exc:
            console.print(f"[red]Invalid vendors config:[/] {exc}")
            raise typer.Exit(code=2) from exc
    return _VENDORS


def _load_user_config_safe() -> UserConfig:
    try:
        return load_user_config()
    except ValueError as exc:
        console.print(f"[red]Invalid bakar config:[/] {exc}")
        raise typer.Exit(code=2) from exc


def _version(value: bool) -> None:
    if value:
        console.print(f"bakar {__version__}")
        raise typer.Exit


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version"),
    ] = False,
) -> None:
    global _USER_CONFIG
    _USER_CONFIG = _load_user_config_safe()
    _get_vendors()
