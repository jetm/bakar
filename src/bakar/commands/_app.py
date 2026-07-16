"""Typer app, Rich console, and startup state for all bakar subcommands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from bakar import __version__
from bakar.output_mode import OutputMode
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
_MOLD: bool = False
_MOLD_BASELINE: bool = False
_MOLD_GLOBAL: bool = False
# Human-output mode override from the global --plain/--ci/--rich flags; None means
# auto-detect (see bakar.output_mode.resolve_output_mode). Read by build and monitor.
_OUTPUT_MODE_OVERRIDE: OutputMode | None = None


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
    mold: Annotated[
        bool,
        typer.Option("--mold", help="Enable the mold linker overlay for build and kas subcommands."),
    ] = False,
    mold_baseline: Annotated[
        bool,
        typer.Option(
            "--mold-baseline",
            help="Symmetric bfd baseline arm for link-time measurement over the allow-list; "
            "add --mold-global for the deny-list (whole-image) scope.",
        ),
    ] = False,
    mold_global: Annotated[
        bool,
        typer.Option(
            "--mold-global",
            help=(
                "Enable mold in deny-list mode (MOLD_EXCLUDED_PN) across all target recipes "
                "instead of the default allow-list (mutually exclusive with --mold/--mold-baseline)."
            ),
        ),
    ] = False,
    plain: Annotated[
        bool,
        typer.Option("--plain", "--ci", help="Force plain, ANSI-free output for build and monitor (CI-friendly)."),
    ] = False,
    rich_output: Annotated[
        bool,
        typer.Option("--rich", help="Force the Rich live display even when output is not a TTY."),
    ] = False,
) -> None:
    global _USER_CONFIG, _HIDE_DOCTOR_REPORT, _HOST_MODE, _CONTAINER_MODE, _SCCACHE_DIST
    global _SCCACHE_SCHEDULER, _MOLD, _MOLD_BASELINE, _MOLD_GLOBAL, _OUTPUT_MODE_OVERRIDE
    if plain and rich_output:
        console.print("[red]choose either --plain/--ci or --rich, not both[/]")
        raise typer.Exit(code=2)
    # --mold-global + --mold-baseline together request the bfd baseline arm at
    # global (deny-list) scope, to measure against a global mold build over the
    # same recipe set. Any other multi-flag combination is contradictory.
    global_bfd_baseline = mold_global and mold_baseline and not mold
    if sum([mold, mold_baseline, mold_global]) > 1 and not global_bfd_baseline:
        console.print(
            "[red]choose one of --mold, --mold-baseline, --mold-global "
            "(or --mold-global --mold-baseline for the global bfd baseline)[/]"
        )
        raise typer.Exit(code=2)
    _USER_CONFIG = _load_user_config_safe()
    _HIDE_DOCTOR_REPORT = hide_doctor_report
    _HOST_MODE = host
    _CONTAINER_MODE = container
    _SCCACHE_DIST = sccache_dist
    _SCCACHE_SCHEDULER = sccache_scheduler
    _MOLD = mold
    _MOLD_BASELINE = mold_baseline
    _MOLD_GLOBAL = mold_global
    _OUTPUT_MODE_OVERRIDE = OutputMode.PLAIN if plain else (OutputMode.RICH if rich_output else None)
    _get_vendors()
    _load_presets_safe()
