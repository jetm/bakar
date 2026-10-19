"""Typer app, Rich console, and startup state for all bakar subcommands."""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape

from bakar import __version__, detach_records, package_identity
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
# stderr, deliberately, and it is a convention rather than this module's
# preference: every command's human-readable output goes here, while stdout is
# reserved for machine-readable payloads a caller can pipe.
# ``commands/report.py`` is the exemplar - the same function emits
# ``print(json.dumps(payload))`` for ``--format json`` and ``console.print(...)``
# for the human report, and ``bakar graph`` splits ``--format dot``/``json`` the
# same way.
#
# The consequence is worth stating because it looks like a bug the first time:
# ``bakar insights ... > file`` produces an EMPTY file. The report went to
# stderr, which is correct. Use ``2> file``, or ``2>&1 |`` for both streams.
#
# So a command that gains a machine-readable format prints it to stdout and
# leaves everything else on this console. Moving one command's human output to
# stdout would make it the only one that does, and would collide with that
# format the day it is added.
console = Console(stderr=True)

_VENDORS: list | None = None
_USER_CONFIG: UserConfig | None = None
_PRESETS: list[PresetEntry] | None = None


def _get_vendors() -> list:
    global _VENDORS
    if _VENDORS is None:
        try:
            _VENDORS = load_vendors()
        except ValueError as exc:
            console.print(f"[red]Invalid vendors config:[/] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
    return _VENDORS


def _load_user_config_safe() -> UserConfig:
    try:
        return load_user_config()
    except ValueError as exc:
        console.print(f"[red]Invalid bakar config:[/] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc


def _load_presets_safe() -> None:
    global _PRESETS
    if _PRESETS is None:
        try:
            _PRESETS = load_presets()
        except (ValueError, OSError) as exc:
            console.print(f"[red]Invalid presets config:[/] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc


def _version(value: bool) -> None:
    if value:
        # The content id lets `build --on <host>` detect a remote bakar whose
        # code/overlays differ even when __version__ matches (see package_identity).
        console.print(f"bakar {__version__} ({package_identity()})")
        raise typer.Exit


@dataclass(frozen=True, kw_only=True)
class _GlobalOptions:
    """The top-level callback's flags, packed for ``_main_impl``.

    ``--version`` is absent on purpose: it is an eager option handled entirely by
    the ``_version`` callback, which exits before ``_main`` runs.
    """

    hide_doctor_report: bool
    host: bool
    container: bool
    no_scope: bool
    sccache_dist: bool
    sccache_scheduler: str | None
    mold: bool
    mold_baseline: bool
    mold_global: bool
    plain: bool
    rich_output: bool


# The options of the latest callback invocation.
_GLOBAL_OPTIONS: _GlobalOptions = _GlobalOptions(
    hide_doctor_report=False,
    host=False,
    container=False,
    no_scope=False,
    sccache_dist=False,
    sccache_scheduler=None,
    mold=False,
    mold_baseline=False,
    mold_global=False,
    plain=False,
    rich_output=False,
)


_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_ORIGINAL_SHOWWARNING = warnings.showwarning


def _bakar_showwarning(message, category, filename, lineno, file=None, line=None):
    """Render a warning raised from inside bakar as one ``bakar: warning:`` line.

    Warnings attributed to other code keep the stock rendering, so a
    third-party deprecation still shows its source location.
    """
    try:
        inside = Path(filename).resolve().is_relative_to(_PACKAGE_DIR)
    except OSError, ValueError:
        inside = False
    if not inside:
        _ORIGINAL_SHOWWARNING(message, category, filename, lineno, file, line)
        return
    stream = file if file is not None else sys.stderr
    try:
        stream.write(f"bakar: warning: {message}\n")
    except OSError:
        pass


def _install_warning_format() -> None:
    if warnings.showwarning is not _bakar_showwarning:
        warnings.showwarning = _bakar_showwarning


def _main_impl(opts: _GlobalOptions) -> None:
    """Validate the global flags and publish them as ``_GLOBAL_OPTIONS``.

    Readers go through ``_GLOBAL_OPTIONS`` (``global_host_mode()`` and friends in
    commands/_helpers.py), so the object must be published here or every reader
    stays on defaults.
    """
    global _USER_CONFIG
    global _GLOBAL_OPTIONS
    # First, so no later step (or a validation exit) lets children inherit it.
    detach_records.consume_registration_env()
    _install_warning_format()
    if opts.plain and opts.rich_output:
        console.print("[red]choose either --plain/--ci or --rich, not both[/]")
        raise typer.Exit(code=2)
    # --mold-global + --mold-baseline together request the bfd baseline arm at
    # global (deny-list) scope, to measure against a global mold build over the
    # same recipe set. Any other multi-flag combination is contradictory.
    global_bfd_baseline = opts.mold_global and opts.mold_baseline and not opts.mold
    if sum([opts.mold, opts.mold_baseline, opts.mold_global]) > 1 and not global_bfd_baseline:
        console.print(
            "[red]choose one of --mold, --mold-baseline, --mold-global "
            "(or --mold-global --mold-baseline for the global bfd baseline)[/]"
        )
        raise typer.Exit(code=2)
    _USER_CONFIG = _load_user_config_safe()
    _GLOBAL_OPTIONS = opts
    _get_vendors()
    _load_presets_safe()


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
            help="Force the host path, overriding a container toggle from BAKAR_CONTAINER or config "
            "(host is the default, so alone this changes nothing; --container outranks it).",
        ),
    ] = False,
    container: Annotated[
        bool,
        typer.Option(
            "--container",
            help="Opt into kas-container instead of the host path (applies to build and all kas subcommands).",
        ),
    ] = False,
    no_scope: Annotated[
        bool,
        typer.Option(
            "--no-scope",
            help="Run the build directly instead of inside a transient systemd scope "
            "(disables session-survival and the cgroup memory ceiling).",
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
                "instead of the default allow-list (mutually exclusive with --mold; combine with --mold-baseline "
                "for the global bfd baseline)."
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
    _main_impl(
        _GlobalOptions(
            hide_doctor_report=hide_doctor_report,
            host=host,
            container=container,
            no_scope=no_scope,
            sccache_dist=sccache_dist,
            sccache_scheduler=sccache_scheduler,
            mold=mold,
            mold_baseline=mold_baseline,
            mold_global=mold_global,
            plain=plain,
            rich_output=rich_output,
        )
    )
