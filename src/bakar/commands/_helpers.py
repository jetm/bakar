"""Shared helpers used across bakar subcommands.

Pure functions and display utilities that do not themselves register
Typer commands. Every subcommand module imports from here rather than
from ``cli``.
"""

from __future__ import annotations

import importlib.resources
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer
from rich.table import Table

from bakar.bsp_detect import detect_bsp_from_yaml, detect_kas_workspace, is_bbsetup_workspace
from bakar.bsp_model import BspModel, detect_bsp_family, get_model
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.layers import collect_layer_hashes

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.layers import LayerHash

# ---------------------------------------------------------------------------
# Workspace detection
# ---------------------------------------------------------------------------


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
    or ``workspace/ti/``; otherwise ``None``.
    """
    cwd = Path.cwd().resolve()
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


def _tuning_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return all opt-in tuning overlay paths for cfg (hashequiv + shared-cache)."""
    return [*_hashequiv_extra_overlays(cfg), *_shared_cache_extra_overlays(cfg)]


# ---------------------------------------------------------------------------
# Build-directory cleanup
# ---------------------------------------------------------------------------


def _clean_build_dir(cfg: BuildConfig) -> None:
    """Remove the BSP-specific ``build/`` dir. Shared by ``bakar clean``
    and ``bakar build --clean``. No-op if the dir is already absent.
    """
    import shutil

    from bakar.commands import console

    build_dir = cfg.bsp_root / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
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
            "The YAML must declare at least a machine: value or a repos: block. "
            "See kas's documentation for the schema.",
            markup=True,
        )
        raise typer.Exit(code=2)
    if family == "generic":
        return ("generic", None)
    return (family, get_model(family))


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
    for r in results:
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
            r.name,
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


def _print_layer_hashes(cfg: BuildConfig, hashes: list[LayerHash] | None = None) -> None:
    """Print a ``layers:`` table of repo, short hash, and branch.

    Collects layer hashes via ``collect_layer_hashes(cfg)`` when ``hashes``
    is ``None``; otherwise reuses the precomputed list so the caller can
    avoid a second per-repo git query.

    Prints nothing when no layer hashes are available (no
    ``bblayers.conf`` yet, or every repo skipped).
    """
    from bakar.commands import console

    if hashes is None:
        hashes = collect_layer_hashes(cfg)
    if not hashes:
        return
    console.print("layers:", highlight=False)
    width = max(len(h.repo) for h in hashes)
    for h in hashes:
        branch = f"  ({h.branch})" if h.branch else ""
        ver = f"  {h.version}" if h.version else ""
        console.print(f"  {h.repo:<{width}}  {h.short_hash}{branch}{ver}", highlight=False)


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
