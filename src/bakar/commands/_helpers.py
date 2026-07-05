"""Shared helpers used across bakar subcommands.

Pure functions and display utilities that do not themselves register
Typer commands. Every subcommand module imports from here rather than
from ``cli``.
"""

from __future__ import annotations

import importlib.resources
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer
from rich.table import Table

from bakar.bsp_detect import detect_bsp_from_yaml, detect_kas_workspace, is_bbsetup_workspace
from bakar.bsp_model import BspModel, detect_bsp_family, get_model
from bakar.diagnostics import CheckResult, Severity, Status, any_blocking_failure, group_results, run_all
from bakar.layers import collect_layer_hashes

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.layers import LayerHash
    from bakar.output_mode import OutputMode

# ---------------------------------------------------------------------------
# Workspace detection
# ---------------------------------------------------------------------------


_WORKSPACE_HELP = "Workspace root; auto-detected if omitted"

# The invoking cwd captured before ``_enter_workspace`` chdirs into a ``-w``
# workspace. ``_bsp_from_cwd`` reads it so family auto-detection reflects where
# the user actually stood (e.g. ``<ws>/ti``) rather than the post-chdir workspace
# root. Reset on every command invocation (the callback always fires), so it never
# leaks a stale cwd into a later ``-w``-less command.
_INVOCATION: dict[str, Path] = {}


def _enter_workspace(workspace: Path | None) -> Path | None:
    """Resolve, validate, and chdir into an explicit ``-w``/``--workspace`` path.

    Returns ``None`` unchanged (no chdir) so commands without ``-w`` keep their
    CWD-based behavior. Otherwise resolves ``workspace`` to an absolute path,
    ``chdir``s into it, and returns it so a relative positional argument
    resolves against the workspace instead of the original CWD. A missing path
    or a non-directory raises :class:`typer.BadParameter`, which Typer renders
    as exit 2 naming the option.
    """
    _INVOCATION.pop("cwd", None)
    if workspace is None:
        return None
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise typer.BadParameter(
            f"workspace does not exist or is not a directory: {resolved}",
            param_hint="--workspace/-w",
        )
    _INVOCATION["cwd"] = Path.cwd()
    os.chdir(resolved)
    return resolved


def _workspace_callback(value: Path | None) -> Path | None:
    """Typer parameter callback: chdir into ``value`` before the command body runs."""
    return _enter_workspace(value)


WorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", "-w", callback=_workspace_callback, help=_WORKSPACE_HELP, is_eager=True),
]


def _bbsetup_workspace(workspace: Path | None) -> Path | None:
    """Return the setup dir for an initialized bitbake-setup workspace, else None.

    With an explicit ``-w`` the path is checked as-is. Without it, the cwd and
    its parents are walked (mirroring ``_workspace_from_cwd``) so the command
    works from a subdirectory of the workspace.
    """

    if workspace is not None:
        return workspace.resolve() if is_bbsetup_workspace(workspace) else None
    cur = Path.cwd().resolve()
    for cand in (cur, *cur.parents):
        if is_bbsetup_workspace(cand):
            return cand
    return None


def _uninitialized_bbsetup_dir(workspace: Path | None) -> Path | None:
    """Return a dir carrying the bitbake-setup signature but not yet initialized.

    A directory with ``config/config-upstream.json`` looks like a bitbake-setup
    workspace; if it lacks ``build/init-build-env`` it has not been initialized
    (``bitbake-setup init`` writes that file). Returns the first such directory
    found (the given workspace, or walking up from cwd), or None when no
    bitbake-setup signature is present or the workspace is fully initialized.
    """
    if workspace is not None:
        cands: tuple[Path, ...] = (workspace.resolve(),)
    else:
        cur = Path.cwd().resolve()
        cands = (cur, *cur.parents)
    for cand in cands:
        if (cand / "config" / "config-upstream.json").exists():
            return None if is_bbsetup_workspace(cand) else cand
    return None


def _find_workspace_from_cwd() -> Path | None:
    """Walk up from CWD to find the BSP workspace root, or None if none found.

    Checks in order:
    1. A .bakar.toml marker file in the candidate directory.
    2. An nxp/ or ti/ subdirectory in the candidate directory.
    3. A bitbake-setup workspace (config/config-upstream.json + build/init-build-env).

    Non-raising counterpart of :func:`_workspace_from_cwd`, for callers that
    treat "not in a workspace" as a skip rather than an error.
    """
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".bakar.toml").is_file():
            return candidate
        if (candidate / "nxp").is_dir() or (candidate / "ti").is_dir():
            return candidate
        if is_bbsetup_workspace(candidate):
            return candidate
    return None


def _workspace_from_cwd() -> Path:
    """Walk up from CWD to find the BSP workspace root, or exit with a message."""
    found = _find_workspace_from_cwd()
    if found is not None:
        return found

    from bakar.commands import console

    console.print(
        "[red]Not inside a BSP workspace[/] (no .bakar.toml or nxp/ / ti/ found). "
        "cd to the workspace root, pass --workspace, or - for generic kas YAMLs - run "
        "`bakar build <kas.yml>` from anywhere."
    )
    raise typer.Exit(code=2)


def _resolve_workspace(
    workspace: Path | None,
    *,
    kas_yaml: Path | None = None,
    family: Literal["nxp", "ti", "generic"] | None = None,
) -> Path:
    """Resolve the workspace path with a BYO+generic carve-out.

    Generic mode (``bakar build my.yml`` where ``my.yml`` does not
    target an NXP/TI SoM) does not own a workspace subtree - the
    overlay symlink and per-run state land next to the user's YAML.
    Skip the cwd walk in that case so generic builds work from any
    directory.
    """
    if workspace is not None:
        return workspace
    if family == "generic" and kas_yaml is not None:
        # For meta-avocado YAMLs this returns the parent of the
        # meta-avocado/ dir (e.g. sources/). For all other generic
        # YAMLs it returns yaml.parent - same as the old behaviour.
        return detect_kas_workspace(kas_yaml)
    return _workspace_from_cwd()


def _bsp_from_cwd(workspace: Path) -> Literal["nxp", "ti"] | None:
    """Detect BSP family from the current working directory.

    Returns ``"nxp"`` or ``"ti"`` if cwd is inside ``workspace/nxp/``
    or ``workspace/ti/``; otherwise ``None``. Under an explicit ``-w`` the
    ``_enter_workspace`` callback has already chdir'd into the workspace root, so
    the pre-chdir invoking cwd (captured in ``_INVOCATION``) is used instead of the
    live cwd; without ``-w`` the live cwd is used exactly as before.
    """
    cwd = _INVOCATION.get("cwd", Path.cwd()).resolve()
    try:
        rel = cwd.relative_to(workspace.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] == "nxp":
        return "nxp"
    if parts[0] == "ti":
        return "ti"
    return None


# ---------------------------------------------------------------------------
# Overlay lookup
# ---------------------------------------------------------------------------


def _overlay_dir() -> Path:
    """Locate the ``overlays/`` package data directory.

    Uses ``importlib.resources`` so the lookup works for both editable
    installs (source tree) and wheel installs (site-packages).
    ``uv_build`` includes all non-``.py`` files under ``src/bakar/``
    automatically, so the YAMLs land at ``bakar/overlays/`` in the wheel.
    """
    return Path(str(importlib.resources.files("bakar") / "overlays"))


def _overlay_for(bsp: BspModel | None) -> Path:
    """Return the absolute path to the static tuning overlay.

    ``bsp=None`` selects ``bakar-tuning-generic.yml`` - the BSP-agnostic
    overlay used by the ``bakar build my.yml`` flow when the YAML does
    not classify as NXP or TI.
    """
    filename = bsp.tuning_overlay_filename if bsp is not None else "bakar-tuning-generic.yml"
    path = _overlay_dir() / filename
    if not path.is_file():
        raise typer.BadParameter(f"tuning overlay missing: {path}. Reinstall bakar or restore the overlays/ directory.")
    return path


def _conditional_overlay(flag: bool, filename: str) -> list[Path]:
    """Return ``[<overlay-dir>/<filename>]`` when *flag* is True and the file exists, else ``[]``."""
    if not flag:
        return []
    path = _overlay_dir() / filename
    return [path] if path.is_file() else []


def _hashequiv_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the hashequiv overlay path when ``cfg.use_hashequiv`` is True."""
    return _conditional_overlay(cfg.use_hashequiv, "bakar-tuning-hashequiv.yml")


def _shared_cache_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the shared-cache overlay path when ``cfg.use_shared_cache`` is True."""
    return _conditional_overlay(cfg.use_shared_cache, "bakar-tuning-shared-cache.yml")


def _sccache_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the sccache overlay path when ``cfg.use_sccache_dist`` is True."""
    return _conditional_overlay(cfg.use_sccache_dist, "bakar-tuning-sccache.yml")


def _ccache_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the ccache overlay path whenever ``[build] ccache`` is on.

    Gated on the raw ``cfg.ccache`` toggle, NOT ``cfg.use_ccache`` (which stays
    the parallelism-dominant-launcher marker). ccache and sccache are
    complementary under the hybrid: with sccache-dist on, the ccache overlay is
    co-selected so the non-allowlisted recipe tail still gets a local object
    cache while sccache distributes the allowlisted heavy recipes. Ordered before
    the sccache overlay in ``_tuning_extra_overlays`` (lower ``zz-bakar-NN`` key)
    so ``INHERIT += "ccache"`` lands before ``INHERIT += "sccache"`` and
    sccache.bbclass's per-recipe ``CCACHE`` override wins for allowlisted PNs.
    """
    return _conditional_overlay(cfg.ccache, "bakar-tuning-ccache.yml")


def _host_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the host-mode isolation overlay path when building in host mode.

    Adds the meta-bakar-host layer (rpm bbappend disabling rpm transaction
    plugins) so rpm-native does not dlopen the build host's ABI-incompatible
    /usr/lib/rpm-plugins during do_rootfs. Container builds run on a clean image
    with no host rpm, so this is gated on ``cfg.host_mode``.
    """
    return _conditional_overlay(cfg.host_mode, "bakar-tuning-host.yml")


def _tuning_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return all opt-in tuning overlay paths for cfg.

    host (host-mode rpm isolation) + ccache (when effective) + hashequiv +
    shared-cache + sccache. List order does not set local.conf precedence - kas
    sorts local_conf_header by key, and the bakar overlays use sort-last
    ``zz-bakar-NN-*`` keys so the numeric segment decides (base < ccache <
    hashequiv < shared-cache < sccache). The host overlay adds only a layer (no
    local_conf_header), so its position is immaterial."""
    return [
        *_host_extra_overlays(cfg),
        *_ccache_extra_overlays(cfg),
        *_hashequiv_extra_overlays(cfg),
        *_shared_cache_extra_overlays(cfg),
        *_sccache_extra_overlays(cfg),
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
    """Apply the global ``--sccache-dist`` / ``--sccache-scheduler`` flags to cfg.

    Mirrors the per-command threading build used before these became global
    callback options: enable the sccache overlay and, when given, point the
    client at the scheduler URL. A no-op when neither global flag is set.
    """
    import bakar.commands._app as _state

    if _state._SCCACHE_DIST:
        cfg = replace(cfg, sccache_dist=True)
    if _state._SCCACHE_SCHEDULER is not None:
        cfg = replace(cfg, sccache_scheduler_url=_state._SCCACHE_SCHEDULER)
    return cfg


def _combine_overlays_with_tuning(user_extras: list[Path], cfg: BuildConfig) -> list[Path]:
    """Append cfg's opt-in tuning overlays to user_extras, deduping by resolved path.

    User-supplied colon overlays come first; tuning overlays land last so they win
    in kas merge order (the sccache/hashequiv overlays do ``INHERIT:remove`` after
    the base config's ``INHERIT +=``). Mirrors the BYO combine in ``build.py`` so
    inspection commands (``dump``, ``getvar``) flatten the same overlay set the
    build actually runs.
    """
    combined = list(user_extras)
    seen = {p.resolve() for p in combined}
    for overlay in _tuning_extra_overlays(cfg):
        resolved = overlay.resolve()
        if resolved not in seen:
            combined.append(overlay)
            seen.add(resolved)
    return combined


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

    build_dir = cfg.bsp_root / "build"
    if build_dir.exists():
        parallel_rmtree(build_dir, description=f"Removing {build_dir.name}/")
        console.print(f"[green]removed[/] {build_dir}")


# ---------------------------------------------------------------------------
# BSP dispatch
# ---------------------------------------------------------------------------


def _dispatch_bsp(manifest_arg: str | None) -> tuple[Literal["nxp", "ti"], BspModel]:
    """Detect the BSP family from the manifest filename and return ``(family, model)``.

    Inspects ``--manifest`` first, then ``BAKAR_MANIFEST``, then falls
    back to the NXP default. Refuses unrecognized shapes with a
    typer.Exit(2) and a hint pointing at the versioning references.
    """
    from bakar.commands import console
    from bakar.config import DEFAULT_NXP_MANIFEST

    pre = manifest_arg or os.environ.get("BAKAR_MANIFEST") or DEFAULT_NXP_MANIFEST
    family = detect_bsp_family(Path(pre), config_file=None)
    if family == "unknown":
        console.print(
            "[red]Unrecognized manifest shape:[/red] "
            f"{pre!r} matches neither NXP (imx-A.B.C-X.Y.Z.xml) nor TI "
            "(processor-sdk-...-config_var<N>.txt / arago-*.txt). "
            "Check the manifest filename format.",
            markup=True,
        )
        raise typer.Exit(code=2)
    return family, get_model(family)


def _dispatch_from_yaml(yaml_path: Path) -> tuple[Literal["nxp", "ti", "generic"], BspModel | None]:
    """Detect the BSP family from a kas YAML and return ``(family, model)``.

    Used by the BYO ``bakar build my.yml`` path. Inspects the YAML's
    ``machine:`` and ``repos:`` blocks via
    :func:`bakar.bsp_detect.detect_bsp_from_yaml`. Returns the
    matching :class:`BspModel` for NXP/TI and ``None`` for generic
    builds (no BspModel applies; the caller layers
    ``bakar-tuning-generic.yml`` and skips vendor-specific pipeline
    steps). Refuses ``"unknown"`` shapes (empty / unparseable YAMLs)
    with a typer.Exit(2).
    """
    from bakar.commands import console

    if not yaml_path.is_file():
        console.print(f"[red]kas YAML not found:[/red] {yaml_path}")
        raise typer.Exit(code=2)
    family = detect_bsp_from_yaml(yaml_path)
    if family == "unknown":
        console.print(
            f"[red]Could not parse {yaml_path} as a kas build.[/red] "
            "The YAML must declare at least a machine: value, a repos: block, "
            "or a header.includes list. See kas's documentation for the schema.",
            markup=True,
        )
        raise typer.Exit(code=2)
    if family == "generic":
        return ("generic", None)
    return (family, get_model(family))


def split_kas_yaml_arg(raw: str | Path | None) -> tuple[Path | None, list[Path]]:
    """Split a colon-joined kas YAML arg into (head, extras), validating each segment.

    Mirrors kas config.py:53-54: splits on ':', resolves each segment to an
    absolute path, checks it exists. Returns (None, []) for None input.
    Exits with code 2 naming any missing segment.
    """
    if raw is None:
        return None, []
    from bakar.commands._app import console

    parts = str(raw).split(":")
    resolved: list[Path] = []
    for part in parts:
        p = Path(part).resolve()
        if not p.is_file():
            console.print(f"[red]kas YAML not found:[/red] {p}")
            raise typer.Exit(code=2)
        resolved.append(p)
    return resolved[0], resolved[1:]


def _normalize_dispatch(
    kas_yaml: Path | None,
    manifest: str | None,
) -> tuple[str, BspModel | None, Path | None, str | None]:
    """Normalize workspace dispatch args and return ``(family, bsp, kas_yaml, manifest)``.

    Call this instead of ``_dispatch_bsp``/``_dispatch_from_yaml`` directly.
    Handles three cases:
    - Positional ``kas_yaml`` provided: dispatches via :func:`_dispatch_from_yaml`.
    - ``-f <path>.yml`` provided: promotes to ``kas_yaml`` and clears ``manifest``,
      then dispatches via :func:`_dispatch_from_yaml`.  This lets users write
      ``bakar inspect busybox -f meta-avocado/kas/machine/qemux86-64.yml`` and
      have the YAML path flow correctly through to ``config.resolve()``.
    - ``-f <manifest.xml>`` or default: dispatches via :func:`_dispatch_bsp`.

    Returns the *normalized* ``kas_yaml`` and ``manifest`` values alongside
    ``family`` and ``bsp`` so the caller passes correct values to
    ``_resolve_workspace()`` and ``config.resolve()``.
    """
    from bakar.commands._app import console

    # Promote -f <yaml> to the positional kas_yaml so the path flows downstream.
    if manifest is not None and manifest.endswith((".yml", ".yaml")):
        if kas_yaml is not None:
            console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
            raise typer.Exit(code=2)
        kas_yaml = Path(manifest)
        manifest = None

    # Standard mutual-exclusion guard.
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    return family, bsp, kas_yaml, manifest


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _print_diagnosis(results: list[CheckResult]) -> None:
    from bakar.commands import console

    if all(r.status is Status.PASS for r in results):
        console.print(f"doctor: {len(results)}/{len(results)} checks passed")
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
    console.print("sstate summary:", highlight=False)
    console.print(f"  wanted: {sstate['sstate_wanted']}", highlight=False)
    console.print(f"  local: {sstate['sstate_local']}", highlight=False)
    console.print(f"  mirrors: {sstate['sstate_mirrors']}", highlight=False)
    console.print(f"  missed: {sstate['sstate_missed']}", highlight=False)
    console.print(f"  current: {sstate['sstate_current']}", highlight=False)
    console.print(f"  match: {sstate['sstate_match_pct']}%", highlight=False)
    console.print(f"  complete: {sstate['sstate_complete_pct']}%", highlight=False)


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
