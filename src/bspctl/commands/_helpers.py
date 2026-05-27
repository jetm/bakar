"""Shared helpers used across bspctl subcommands.

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

from bspctl.bsp_detect import detect_bsp_from_yaml, detect_kas_workspace, is_bbsetup_workspace
from bspctl.bsp_model import BspModel, detect_bsp_family, get_model
from bspctl.diagnostics import CheckResult, Severity, Status
from bspctl.layers import collect_layer_hashes

if TYPE_CHECKING:
    from bspctl.config import BuildConfig
    from bspctl.layers import LayerHash

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


def _workspace_from_cwd() -> Path:
    """Walk up from CWD to find the BSP workspace root.

    Checks in order:
    1. A .bspctl.toml marker file in the candidate directory.
    2. An nxp/ or ti/ subdirectory in the candidate directory.
    3. A bitbake-setup workspace (config/config-upstream.json + build/init-build-env).
    """
    from bspctl.commands import console

    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".bspctl.toml").is_file():
            return candidate
        if (candidate / "nxp").is_dir() or (candidate / "ti").is_dir():
            return candidate
        if is_bbsetup_workspace(candidate):
            return candidate
    console.print(
        "[red]Not inside a BSP workspace[/] (no .bspctl.toml or nxp/ / ti/ found). "
        "cd to the workspace root, pass --workspace, or - for generic kas YAMLs - run "
        "`bspctl build <kas.yml>` from anywhere."
    )
    raise typer.Exit(code=2)


def _resolve_workspace(
    workspace: Path | None,
    *,
    kas_yaml: Path | None = None,
    family: Literal["nxp", "ti", "generic"] | None = None,
) -> Path:
    """Resolve the workspace path with a BYO+generic carve-out.

    Generic mode (``bspctl build my.yml`` where ``my.yml`` does not
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
    ``uv_build`` includes all non-``.py`` files under ``src/bspctl/``
    automatically, so the YAMLs land at ``bspctl/overlays/`` in the wheel.
    """
    return Path(str(importlib.resources.files("bspctl") / "overlays"))


def _overlay_for(bsp: BspModel | None) -> Path:
    """Return the absolute path to the static tuning overlay.

    ``bsp=None`` selects ``bspctl-tuning-generic.yml`` - the BSP-agnostic
    overlay used by the ``bspctl build my.yml`` flow when the YAML does
    not classify as NXP or TI.
    """
    filename = bsp.tuning_overlay_filename if bsp is not None else "bspctl-tuning-generic.yml"
    path = _overlay_dir() / filename
    if not path.is_file():
        raise typer.BadParameter(
            f"tuning overlay missing: {path}. Reinstall bspctl or restore the overlays/ directory."
        )
    return path


# ---------------------------------------------------------------------------
# Build-directory cleanup
# ---------------------------------------------------------------------------


def _clean_build_dir(cfg: BuildConfig) -> None:
    """Remove the BSP-specific ``build/`` dir. Shared by ``bspctl clean``
    and ``bspctl build --clean``. No-op if the dir is already absent.
    """
    import shutil

    from bspctl.commands import console

    build_dir = cfg.bsp_root / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
        console.print(f"[green]removed[/] {build_dir}")


# ---------------------------------------------------------------------------
# BSP dispatch
# ---------------------------------------------------------------------------


def _dispatch_bsp(manifest_arg: str | None) -> tuple[Literal["nxp", "ti"], BspModel]:
    """Detect the BSP family from the manifest filename and return ``(family, model)``.

    Inspects ``--manifest`` first, then ``BSPCTL_MANIFEST``, then falls
    back to the NXP default. Refuses unrecognized shapes with a
    typer.Exit(2) and a hint pointing at the versioning references.
    """
    from bspctl.commands import console
    from bspctl.config import DEFAULT_NXP_MANIFEST

    pre = manifest_arg or os.environ.get("BSPCTL_MANIFEST") or DEFAULT_NXP_MANIFEST
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

    Used by the BYO ``bspctl build my.yml`` path. Inspects the YAML's
    ``machine:`` and ``repos:`` blocks via
    :func:`bspctl.bsp_detect.detect_bsp_from_yaml`. Returns the
    matching :class:`BspModel` for NXP/TI and ``None`` for generic
    builds (no BspModel applies; the caller layers
    ``bspctl-tuning-generic.yml`` and skips vendor-specific pipeline
    steps). Refuses ``"unknown"`` shapes (empty / unparseable YAMLs)
    with a typer.Exit(2).
    """
    from bspctl.commands import console

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


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _print_diagnosis(results: list[CheckResult]) -> None:
    from bspctl.commands import console

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
    from bspctl.commands import console

    if hashes is None:
        hashes = collect_layer_hashes(cfg)
    if not hashes:
        return
    console.print("layers:")
    width = max(len(h.repo) for h in hashes)
    for h in hashes:
        branch = f"  ({h.branch})" if h.branch else ""
        ver = f"  {h.version}" if h.version else ""
        console.print(f"  {h.repo:<{width}}  {h.short_hash}{branch}{ver}")
