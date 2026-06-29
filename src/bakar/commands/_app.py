"""Typer app, Rich console, and startup state for all bakar subcommands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from bakar import __version__
from bakar.preset_config import PresetEntry, load_presets
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
_PRESETS: list[PresetEntry] | None = None
_HIDE_DOCTOR_REPORT: bool = False

# Global build-mode overrides, set from the top-level callback and read by every
# command that resolves a BuildConfig. They are global (callback) options - passed
# before the subcommand, e.g. ``bakar --host --sccache-dist build my.yml`` - so a
# single flag set applies uniformly to build and the inspection/maintenance
# commands (getvar, dump, bitbake, clean-recipe, rebuild, ...).
_HOST_MODE: bool = False
_CONTAINER_MODE: bool = False
_SCCACHE_DIST: bool = False
_SCCACHE_SCHEDULER: str | None = None


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


def _load_presets_safe() -> None:
    global _PRESETS
    if _PRESETS is None:
        try:
            _PRESETS = load_presets()
        except (ValueError, OSError) as exc:
            console.print(f"[red]Invalid presets config:[/] {exc}")
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
    hide_doctor_report: Annotated[
        bool,
        typer.Option(
            "--hide-doctor-report",
            help="Run pre-flight checks but show output only for build-blocking issues.",
        ),
    ] = False,
    host: Annotated[
        bool,
        typer.Option(
            "--host",
            help="Back-compat alias forcing the host path; host is the default, so this is a no-op.",
        ),
    ] = False,
    container: Annotated[
        bool,
        typer.Option(
            "--container",
            help="Opt into kas-container instead of the host path (applies to build and all kas subcommands).",
        ),
    ] = False,
    sccache_dist: Annotated[
        bool,
        typer.Option("--sccache-dist", help="Enable the sccache-dist overlay for build and kas subcommands."),
    ] = False,
    sccache_scheduler: Annotated[
        str | None,
        typer.Option("--sccache-scheduler", help="sccache-dist scheduler URL, e.g. http://localhost:10600"),
    ] = None,
) -> None:
    global _USER_CONFIG, _HIDE_DOCTOR_REPORT, _HOST_MODE, _CONTAINER_MODE, _SCCACHE_DIST, _SCCACHE_SCHEDULER
    _USER_CONFIG = _load_user_config_safe()
    _HIDE_DOCTOR_REPORT = hide_doctor_report
    _HOST_MODE = host
    _CONTAINER_MODE = container
    _SCCACHE_DIST = sccache_dist
    _SCCACHE_SCHEDULER = sccache_scheduler
    _get_vendors()
    _load_presets_safe()
