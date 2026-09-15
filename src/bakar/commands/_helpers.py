"""Shared helpers used across bakar subcommands.

Pure functions and display utilities that do not themselves register
Typer commands. Every subcommand module imports from here rather than
from ``cli``.

Workspace resolution and BSP dispatch live in :mod:`bakar.commands._workspace`;
overlay resolution lives in :mod:`bakar.commands._overlays`. Both are
re-exported below so every existing ``from bakar.commands._helpers import
...`` call site keeps working unchanged - this module had ~38 importers
across ``commands/*.py`` before the split.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer
from rich.table import Table

from bakar.commands._overlays import (
    _UNINATIVE_FRAGMENT,
    _UNINATIVE_OS_RELEASE,
    _arch_probe_extra_overlays,
    _ccache_extra_overlays,
    _combine_overlays_with_tuning,
    _conditional_overlay,
    _hashequiv_extra_overlays,
    _host_extra_overlays,
    _host_is_arch_like,
    _mold_extra_overlays,
    _overlay_dir,
    _overlay_for,
    _sccache_extra_overlays,
    _shared_cache_extra_overlays,
    _tuning_extra_overlays,
    _uninative_extra_overlays,
)
from bakar.commands._workspace import (
    _INVOCATION,
    _MANIFEST_FAMILIES,
    _WORKSPACE_HELP,
    WorkspaceOption,
    _bbsetup_workspace,
    _bsp_from_cwd,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _enter_workspace,
    _family_from_workspace_contents,
    _find_workspace_from_cwd,
    _normalize_dispatch,
    _resolve_workspace,
    _uninitialized_bbsetup_dir,
    _workspace_callback,
    _workspace_from_cwd,
    invoking_cwd,
    logical_path,
    split_kas_yaml_arg,
)
from bakar.diagnostics import CheckResult, Severity, Status, any_blocking_failure, group_results, run_all
from bakar.layers import collect_layer_hashes

if TYPE_CHECKING:
    from rich.console import Console

    from bakar.bsp_model import BspModel
    from bakar.config import BuildConfig
    from bakar.layers import LayerHash
    from bakar.output_mode import OutputMode

__all__ = [
    "_INVOCATION",
    "_MANIFEST_FAMILIES",
    "_UNINATIVE_FRAGMENT",
    "_UNINATIVE_OS_RELEASE",
    "_WORKSPACE_HELP",
    "CheckResult",
    "Severity",
    "Status",
    "WorkspaceOption",
    "_SstateRender",
    "_arch_probe_extra_overlays",
    "_bbsetup_workspace",
    "_bsp_from_cwd",
    "_ccache_extra_overlays",
    "_clean_build_dir",
    "_combine_overlays_with_tuning",
    "_conditional_overlay",
    "_dispatch_bsp",
    "_dispatch_from_yaml",
    "_enter_workspace",
    "_family_from_workspace_contents",
    "_find_run",
    "_find_workspace_from_cwd",
    "_hashequiv_extra_overlays",
    "_host_extra_overlays",
    "_host_is_arch_like",
    "_mold_extra_overlays",
    "_normalize_dispatch",
    "_overlay_dir",
    "_overlay_for",
    "_print_diagnosis",
    "_print_layer_hashes",
    "_print_sstate_summary",
    "_render_sstate_lines",
    "_resolve_workspace",
    "_run_doctor_gate",
    "_sccache_extra_overlays",
    "_shared_cache_extra_overlays",
    "_tuning_extra_overlays",
    "_uninative_extra_overlays",
    "_uninitialized_bbsetup_dir",
    "_workspace_callback",
    "_workspace_from_cwd",
    "apply_mold_overrides",
    "apply_sccache_overrides",
    "apply_scope_override",
    "global_container_mode",
    "global_host_mode",
    "global_no_scope",
    "global_output_mode_override",
    "global_sccache_dist_override",
    "invoking_cwd",
    "logical_path",
    "split_kas_yaml_arg",
]


def global_host_mode() -> bool:
    """Return the global ``--host`` flag set on the top-level callback.

    A late import avoids a circular dependency between ``_helpers`` and ``_app``.
    """
    import bakar.commands._app as _state

    return _state._HOST_MODE


def global_container_mode() -> bool:
    """Return the global ``--container`` flag set on the top-level callback.

    A late import avoids a circular dependency between ``_helpers`` and ``_app``.
    """
    import bakar.commands._app as _state

    return _state._CONTAINER_MODE


def global_output_mode_override() -> OutputMode | None:
    """Return the global ``--plain``/``--ci``/``--rich`` override, or None for auto-detect.

    A late import avoids a circular dependency between ``_helpers`` and ``_app``.
    """
    import bakar.commands._app as _state

    return _state._OUTPUT_MODE_OVERRIDE


def apply_sccache_overrides(cfg: BuildConfig) -> BuildConfig:
    """Apply the global ``--sccache-scheduler`` flag to cfg.

    Now that ``--sccache-dist`` is threaded into ``resolve()`` directly via
    ``sccache_dist_override``, this only points the client at the scheduler
    URL when one is given. A no-op when the global flag is not set.
    """
    import bakar.commands._app as _state

    if _state._SCCACHE_SCHEDULER is not None:
        cfg = replace(cfg, sccache_scheduler_url=_state._SCCACHE_SCHEDULER)
    return cfg


def global_no_scope() -> bool:
    """Return the global ``--no-scope`` flag set on the top-level callback.

    A late import avoids a circular dependency between ``_helpers`` and ``_app``.
    """
    import bakar.commands._app as _state

    return _state._NO_SCOPE


def global_sccache_dist_override() -> bool | None:
    """Return the global ``--sccache-dist`` override, or None if not set.

    A late import avoids a circular dependency between ``_helpers`` and ``_app``.
    """
    import bakar.commands._app as _state

    if _state._SCCACHE_DIST:
        return True
    return None


def apply_scope_override(cfg: BuildConfig) -> BuildConfig:
    """Fold the global ``--no-scope`` flag into cfg (disable the transient scope).

    Mirrors ``apply_sccache_overrides``/``apply_mold_overrides``: the callback
    stores the flag on ``_app``; here it clears ``cfg.scope`` so ``run_build`` /
    ``run_shell_live`` launch the build unwrapped. A no-op when the flag is
    unset - the config-resolved default (``[build] scope``) stands.
    """
    if global_no_scope():
        return replace(cfg, scope=False)
    return cfg


def apply_mold_overrides(cfg: BuildConfig) -> BuildConfig:
    """Apply the global ``--mold`` / ``--mold-baseline`` / ``--mold-global`` flags to cfg.

    Mirrors ``apply_sccache_overrides``: the callback stores the flag state in
    module globals on ``_app``; here they are folded into cfg. ``--mold-baseline``
    is the symmetric bfd measurement arm, so it enables mold in ``baseline`` mode;
    ``--mold-global`` enables the deny-list (``MOLD_EXCLUDED_PN``) arm the bbclass
    already implements but which is otherwise unreachable from any bakar surface;
    ``--mold`` enables it in the default ``list`` mode. ``--mold-global`` together
    with ``--mold-baseline`` selects ``baseline-global`` - the bfd arm at deny-list
    scope, to A/B against a ``global`` mold build. A no-op when no flag is set; the
    ``_app`` callback rejects every multi-flag combination except that one pair.
    """
    import bakar.commands._app as _state

    if _state._MOLD_BASELINE and _state._MOLD_GLOBAL:
        cfg = replace(cfg, mold=True, mold_mode="baseline-global")
    elif _state._MOLD_BASELINE:
        cfg = replace(cfg, mold=True, mold_mode="baseline")
    elif _state._MOLD_GLOBAL:
        cfg = replace(cfg, mold=True, mold_mode="global")
    elif _state._MOLD:
        cfg = replace(cfg, mold=True, mold_mode="list")
    return cfg


# ---------------------------------------------------------------------------
# Build-directory cleanup
# ---------------------------------------------------------------------------


def _clean_build_dir(cfg: BuildConfig) -> None:
    """Remove the BSP-specific ``build/`` dir. Shared by ``bakar clean``
    and ``bakar build --clean``. No-op if the dir is already absent.

    Uses :func:`bakar.fsremove.parallel_rmtree` - the same pooled-removal path
    ``clean-cache`` uses for sstate GC - so wiping a multi-hundred-GB ``tmp/``
    parallelizes per-recipe subtree deletion instead of serializing one rmtree.
    """
    from bakar.commands import console
    from bakar.fsremove import parallel_rmtree

    build_dir = cfg.bsp_root / cfg.build_dir_name
    if build_dir.exists():
        parallel_rmtree(build_dir, description=f"Removing {build_dir.name}/")
        console.print(f"[green]removed[/] {build_dir}")

    # A host-mode local_tmpdir_base override relocates the build TMPDIR to
    # node-local disk, outside build_dir; wiping build_dir alone would strand
    # it. is_relative_to is False exactly when the override is active (the
    # unset default resolves under build_dir and was already removed above).
    tmpdir = cfg.resolved_tmpdir
    if not tmpdir.is_relative_to(build_dir) and tmpdir.exists():
        parallel_rmtree(tmpdir, description=f"Removing {tmpdir.name}/ (local TMPDIR)")
        console.print(f"[green]removed[/] {tmpdir}")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


# Above this, a passing check's message is carrying evidence rather than
# restating the condition it just confirmed. See _print_diagnosis.
_PASS_NOTE_MIN_CHARS = 120


def _print_diagnosis(results: list[CheckResult]) -> None:
    from bakar.commands import console

    if all(r.status is Status.PASS for r in results):
        console.print(f"doctor: {len(results)}/{len(results)} checks passed")
        # The all-PASS path prints no table, so any detail a passing check
        # carried would be lost here. The rule: surface a PASS message only
        # when it is longer than _PASS_NOTE_MIN_CHARS. A check that merely
        # confirms its own condition says so in a few words ("no stale locks or
        # sockets"), and reprinting those would bury the summary line; a
        # message that runs long is carrying counted evidence instead - the
        # uninative-leak scan's artifact count and its per-class tally of the
        # dependencies it chose not to report, whose whole safety argument is
        # that nothing is hidden. Rendered with markup enabled, exactly as the
        # table below renders it, because the message arrives pre-neutralized.
        for r in results:
            if len(r.message) > _PASS_NOTE_MIN_CHARS:
                console.print(f"  [bold]{r.name}[/]: {r.message}")
        return
    table = Table(title="Pre-flight diagnosis", show_edge=False)
    table.add_column("Check", no_wrap=True)
    table.add_column("Sev")
    table.add_column("Status")
    table.add_column("Detail")
    for gi, (group_name, group_rows) in enumerate(group_results(results)):
        if gi > 0:
            table.add_section()
        table.add_row(f"[bold cyan]{group_name}[/]", "", "", "")
        for r in group_rows:
            status_colour = {
                Status.PASS: "green",
                Status.FAIL: {
                    Severity.BLOCK: "red",
                    Severity.WARN: "yellow",
                    Severity.INFO: "cyan",
                }[r.severity],
                Status.SKIP: "dim",
            }[r.status]
            table.add_row(
                f"  {r.name}",
                r.severity.value,
                f"[{status_colour}]{r.status.value}[/]",
                r.message,
            )
    console.print(table)
    hints = [r for r in results if r.status is Status.FAIL and r.fix_hint]
    if hints:
        console.print()
        for r in hints:
            console.print(f"[yellow]fix[/] [bold]{r.name}[/]: {r.fix_hint}")


def _run_doctor_gate(cfg: BuildConfig, log, bsp: BspModel | None) -> None:
    """Run pre-flight checks; raise typer.Exit(2) on any blocking failure.

    Checks always run. The full report is printed unless the report is hidden
    (the global ``--hide-doctor-report`` flag or ``[build] show_doctor_report =
    false``), in which case only build-blocking rows are shown. A blocking
    failure aborts the build regardless of whether the report is hidden.
    """
    import bakar.commands._app as _state

    log.step_start("doctor")
    results = run_all(cfg, bsp)
    diag_path = log.run_dir / "diagnosis.txt"
    diag_path.write_text(
        "\n".join(f"{r.severity.value:5} {r.status.value:4} {r.name:22} {r.message}" for r in results) + "\n"
    )
    hide = _state._HIDE_DOCTOR_REPORT or (
        _state._USER_CONFIG is not None and not _state._USER_CONFIG.show_doctor_report
    )
    if hide:
        blocking = [r for r in results if r.severity is Severity.BLOCK and r.status is Status.FAIL]
        if blocking:
            _print_diagnosis(blocking)
    else:
        _print_diagnosis(results)
    if any_blocking_failure(results):
        log.step_fail("doctor", reason="blocking failure")
        raise typer.Exit(code=2)
    log.step_ok("doctor", checks=len(results))


def _print_layer_hashes(cfg: BuildConfig, hashes: list[LayerHash] | None = None) -> None:
    """Print a ``layers:`` table of repo, short hash, and branch.

    Collects layer hashes via ``collect_layer_hashes(cfg)`` when ``hashes``
    is ``None``; otherwise reuses the precomputed list so the caller can
    avoid a second per-repo git query.

    Prints nothing when no layer hashes are available (no
    ``bblayers.conf`` yet, or every repo skipped).
    """
    from bakar.commands import console
    from bakar.layers import layer_hash_table

    if hashes is None:
        hashes = collect_layer_hashes(cfg)
    if not hashes:
        return
    console.print(layer_hash_table(hashes))


@dataclass(frozen=True, kw_only=True)
class _SstateRender:
    """The seven sstate counts plus the two presentation toggles.

    Packed into one object so the five same-typed count fields are named at
    every call site and a transposition becomes unexpressible rather than
    merely untested. ``header_style`` wraps the heading in a Rich markup tag
    (e.g. ``bold``); empty leaves it plain. ``highlight`` toggles Rich number
    highlighting.
    """

    wanted: int | None
    local: int | None
    mirrors: int | None
    missed: int | None
    current: int | None
    match_pct: int | None
    complete_pct: int | None
    header_style: str = ""
    highlight: bool = True


def _render_sstate_lines(console: Console, *, render: _SstateRender) -> None:
    """Render the 7-field sstate summary block to ``console``.

    Shared by the ``report`` command's success summary and
    ``_print_sstate_summary`` so the labels and ordering live in one place.
    """
    highlight = render.highlight
    header = f"[{render.header_style}]sstate summary:[/]" if render.header_style else "sstate summary:"
    console.print(header, highlight=highlight)
    console.print(f"  wanted: {render.wanted}", highlight=highlight)
    console.print(f"  local: {render.local}", highlight=highlight)
    console.print(f"  mirrors: {render.mirrors}", highlight=highlight)
    console.print(f"  missed: {render.missed}", highlight=highlight)
    console.print(f"  current: {render.current}", highlight=highlight)
    console.print(f"  match: {render.match_pct}%", highlight=highlight)
    console.print(f"  complete: {render.complete_pct}%", highlight=highlight)


def _print_sstate_summary(kas_log: Path) -> None:
    """Print the sstate summary from ``kas_log`` when the line is present.

    No-op when the summary line is absent (e.g. a dry-run or an interrupted
    build that never reached the sstate accounting phase).
    """
    from bakar.commands import console
    from bakar.report import _parse_sstate_summary

    sstate = _parse_sstate_summary(kas_log)
    if sstate.get("sstate_wanted") is None:
        return
    _render_sstate_lines(
        console,
        render=_SstateRender(
            wanted=sstate["sstate_wanted"],
            local=sstate["sstate_local"],
            mirrors=sstate["sstate_mirrors"],
            missed=sstate["sstate_missed"],
            current=sstate["sstate_current"],
            match_pct=sstate["sstate_match_pct"],
            complete_pct=sstate["sstate_complete_pct"],
            highlight=False,
        ),
    )


def _find_run(
    runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]],
    run_id: str | None,
) -> tuple[Path, Literal["nxp", "ti", "generic"]] | None:
    """Locate a run directory by ID across the supplied search roots.

    Each entry is a ``(runs_dir, label)`` pair so the caller can mix
    the per-BSP roots (``workspace/nxp/build/runs``,
    ``workspace/ti/build/runs``) with a generic BYO root
    (``<yaml-parent>/build/runs``). With ``run_id=None`` returns the
    most recent run across all roots; with an explicit ID, the first
    matching entry. Returns ``None`` when nothing matches.
    """
    candidates: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = []
    for runs_dir, label in runs_dirs:
        if not runs_dir.is_dir():
            continue
        candidates.extend((entry, label) for entry in runs_dir.iterdir() if entry.is_dir())

    if not candidates:
        return None

    if run_id is None:
        candidates.sort(key=lambda pair: pair[0].name, reverse=True)
        return candidates[0]

    for run_dir, label in candidates:
        if run_dir.name == run_id:
            return (run_dir, label)
    return None


def _run_started_epoch(run_dir: Path) -> float | None:
    """Best-effort build start time (epoch seconds) from the run-dir name.

    bitbake's BuildStarted event carries no timestamp, so the event log cannot
    supply one. The run directory is named ``YYYYMMDD-HHMMSS-<pid>`` at the
    local wall-clock start (the pid suffix disambiguates two builds started in
    the same second - see RunLogger.run_id), so parse the leading 15-char
    timestamp and ignore the rest. Returns None when the name does not parse.
    """
    try:
        return time.mktime(time.strptime(run_dir.name[:15], "%Y%m%d-%H%M%S"))
    except ValueError, OverflowError:
        return None
