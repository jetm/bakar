"""`bspctl` CLI entry point.

Subcommands:

* ``bspctl build [<kas.yml>]``  - one-shot orchestration. With a positional
  YAML (BYO form) it skips sync/setup-env/gen-kas and goes straight to
  the kas-container build with the static tuning overlay layered on
  top. Without it, the manifest-driven pipeline runs (sync -> setup-env
  -> gen-kas -> overlay -> build).
* ``bspctl sync``     - run the manifest-driven sync (repo init+sync for
  NXP, oe-layertool populate for TI) and bblayers regeneration. Useful
  when you want to refresh ``sources/`` without building.
* ``bspctl doctor``   - run the diagnosis standalone
* ``bspctl triage``   - post-mortem the last (or named) run
* ``bspctl shell``    - drop into a kas-container shell for the project
* ``bspctl gen-kas``  - regenerate the topology-only kas YAML (no tuning)
* ``bspctl clean``    - wipe the build/ directory
* ``bspctl log``      - tail the latest run's kas.log live

The build pipeline routes through :class:`bspctl.bsp_model.BspModel`
- the ``_dispatch_bsp`` helper inspects the manifest filename and the
``_dispatch_from_yaml`` helper inspects a kas YAML; both return the
matching model. NXP and TI share the same set of subcommands; only
the dispatched callables differ.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from bspctl import __version__
from bspctl.bsp_detect import detect_bsp_from_yaml, detect_kas_workspace
from bspctl.bsp_model import BspModel, detect_bsp_family, get_model
from bspctl.config import BuildConfig, resolve
from bspctl.diagnostics import (
    CheckResult,
    Severity,
    Status,
    any_blocking_failure,
    run_all,
)
from bspctl.kas import KasGenOptions, write_yaml
from bspctl.observability import RunLogger
from bspctl.steps import bitbake_override as step_override
from bspctl.steps import kas_build as step_kas
from bspctl.steps import run_qemu as step_run
from bspctl.steps import stress_parse as step_stress_parse
from bspctl.triage import analyse
from bspctl.vendor_config import load_vendors
from bspctl.workspace import detect

app = typer.Typer(
    help="BSP orchestrator (Variscite NXP + TI built-in).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console()

_VENDORS: list | None = None


def _get_vendors() -> list:
    global _VENDORS
    if _VENDORS is None:
        try:
            _VENDORS = load_vendors()
        except ValueError as exc:
            console.print(f"[red]Invalid vendors config:[/] {exc}")
            raise typer.Exit(code=2) from exc
    return _VENDORS


def _version(value: bool) -> None:
    if value:
        console.print(f"bspctl {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version"),
    ] = False,
) -> None:
    _get_vendors()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workspace_from_cwd() -> Path:
    """Walk up from CWD to find the BSP workspace root.

    Checks in order:
    1. A .bspctl.toml marker file in the candidate directory.
    2. An nxp/ or ti/ subdirectory in the candidate directory.
    """
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".bspctl.toml").is_file():
            return candidate
        if (candidate / "nxp").is_dir() or (candidate / "ti").is_dir():
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
    target a Variscite SoM) does not own a workspace subtree - the
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


def _overlay_dir() -> Path:
    """Locate the ``overlays/`` directory at the varis repo root.

    cli.py lives at ``src/bspctl/cli.py``; the repo root is three
    parents up. Editable installs land here; wheel installs would need
    package_data and an importlib.resources lookup.
    """
    return Path(__file__).resolve().parent.parent.parent / "overlays"


def _overlay_for(bsp: BspModel | None) -> Path:
    """Return the absolute path to the static tuning overlay.

    ``bsp=None`` selects ``bspctl-tuning-generic.yml`` - the BSP-agnostic
    overlay used by the ``bspctl build my.yml`` flow when the YAML does
    not classify as NXP or TI.
    """
    filename = bsp.tuning_overlay_filename if bsp is not None else "bspctl-tuning-generic.yml"
    path = _overlay_dir() / filename
    if not path.is_file():
        raise typer.BadParameter(f"tuning overlay missing: {path}. Reinstall varis or restore the overlays/ directory.")
    return path


def _clean_build_dir(cfg: BuildConfig) -> None:
    """Remove the BSP-specific ``build/`` dir. Shared by ``bspctl clean``
    and ``bspctl build --clean``. No-op if the dir is already absent.
    """
    import shutil

    build_dir = cfg.bsp_root / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
        console.print(f"[green]removed[/] {build_dir}")


def _dispatch_bsp(manifest_arg: str | None) -> tuple[Literal["nxp", "ti"], BspModel]:
    """Detect the BSP family from the manifest filename and return ``(family, model)``.

    Inspects ``--manifest`` first, then ``BSPCTL_MANIFEST``, then falls
    back to the NXP default. Refuses unrecognized shapes with a
    typer.Exit(2) and a hint pointing at the versioning references.
    """
    from bspctl.config import DEFAULT_NXP_MANIFEST

    pre = manifest_arg or os.environ.get("BSPCTL_MANIFEST") or DEFAULT_NXP_MANIFEST
    family = detect_bsp_family(Path(pre), config_file=None)
    if family == "unknown":
        console.print(
            "[red]Unrecognized manifest shape:[/red] "
            f"{pre!r} matches neither NXP (imx-A.B.C-X.Y.Z.xml) nor TI "
            "(processor-sdk-...-config_var<N>.txt / arago-*.txt). "
            "See kb/reference/{nxp,ti}-variscite-bsp-versioning.md.",
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
    ``bspctl-tuning-generic.yml`` and skips Variscite-specific pipeline
    steps). Refuses ``"unknown"`` shapes (empty / unparseable YAMLs)
    with a typer.Exit(2).
    """
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


def _print_diagnosis(results: list[CheckResult]) -> None:
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


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; when supplied, BSP family is inferred from it instead of --manifest.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Run every diagnostic check and exit non-zero on BLOCK failures."""
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    cfg = resolve(
        workspace=_resolve_workspace(workspace, kas_yaml=kas_yaml, family=family),
        bsp_family=family,
        manifest=manifest,
        kas_yaml=kas_yaml,
    )
    results = run_all(cfg, bsp)
    _print_diagnosis(results)
    if any_blocking_failure(results):
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@app.command()
def build(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO form). When set, sync/setup-env/gen-kas are skipped.",
        ),
    ] = None,
    machine: Annotated[str | None, typer.Option("--machine", "-m", help="e.g. imx8mp-var-dart, am62x-var-som")] = None,
    distro: Annotated[str | None, typer.Option("--distro", "-d", help="e.g. fsl-imx-xwayland, arago")] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", "-i", help="e.g. core-image-minimal, var-thin-image"),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option(
            "--manifest",
            "-f",
            help="manifest filename (NXP imx-*.xml or TI processor-sdk-*-config_var<N>.txt)",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="branch override; inferred from manifest filename when omitted",
        ),
    ] = None,
    skip_sync: Annotated[
        bool, typer.Option("--skip-sync", help="Skip sync (repo init+sync for NXP, oe-layertool for TI)")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Regenerate YAML and exit before kas-container build")
    ] = False,
    skip_doctor: Annotated[
        bool,
        typer.Option("--skip-doctor", help="Skip the pre-flight diagnosis (not recommended)"),
    ] = False,
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Remove <bsp>/build/ before running the pipeline (forces a from-scratch build).",
        ),
    ] = False,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Run the build pipeline idempotently.

    Two forms:

    * **BYO**: ``bspctl build my.yml`` - skip sync/setup-env/gen-kas,
      apply the static tuning overlay, run kas-container. The YAML is
      classified as NXP, TI, or generic (a kas YAML that does not
      target a Variscite SoM). Generic mode picks
      ``bspctl-tuning-generic.yml`` and skips the bitbake-override step
      since that swaps the Variscite-bundled bitbake.
    * **Manifest-driven**: ``bspctl build -f imx-6.12.49-2.2.0.xml -m imx95-var-dart`` -
      run sync, setup-env, gen-kas (topology-only), then apply overlay
      and build. Same flag surface as before, just with the optimization
      stack moved to the overlay file.

    The two forms are mutually exclusive: passing both a positional
    YAML and ``--manifest`` exits non-zero.
    """
    byo_form = kas_yaml is not None
    if byo_form and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if byo_form:
        family, bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg: BuildConfig = resolve(
        workspace=ws,
        bsp_family=family,
        machine=machine,
        distro=distro,
        image=image,
        manifest=manifest,
        repo_branch=branch,
        kas_yaml=kas_yaml,
    )

    overlay_source = _overlay_for(bsp)

    label = f"BYO {kas_yaml}" if byo_form else f"{cfg.machine} / {cfg.distro} / {cfg.image}"
    console.print(f"[bold]::[/] bspctl build [{family}] {label}")

    if clean:
        _clean_build_dir(cfg)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        # Record both the dispatched family and the input mode so triage
        # can reconstruct the run without a manifest.
        log.info(
            f"build mode={'byo' if byo_form else 'manifest'} bsp={family} yaml={cfg.kas_yaml} overlay={overlay_source}",
        )

        # 0. diagnosis
        if not skip_doctor:
            log.step_start("doctor")
            results = run_all(cfg, bsp)
            diag_path = log.run_dir / "diagnosis.txt"
            diag_path.write_text(
                "\n".join(f"{r.severity.value:5} {r.status.value:4} {r.name:22} {r.message}" for r in results) + "\n"
            )
            _print_diagnosis(results)
            if any_blocking_failure(results):
                log.step_fail("doctor", reason="blocking failure")
                raise typer.Exit(code=2)
            log.step_ok("doctor", checks=len(results))

        if byo_form:
            # BYO path: skip sync, setup-env, gen-kas - the YAML is what
            # the user gave us. Still apply bitbake-override for NXP/TI
            # (idempotent, swaps the Variscite-bundled bitbake for an
            # upstream clone with the parser fork-race fix). Generic
            # mode skips the override because it targets paths
            # (sources/poky/bitbake or sources/bitbake) that the
            # generic YAML does not own; PYTHONMALLOC=malloc in the
            # generic overlay carries the parser-race mitigation.
            if family == "generic":
                log.step_skip("bitbake_override", reason="generic mode")
            else:
                step_override.apply(cfg, log)
        else:
            # 1. sync (repo for NXP, oe-layertool for TI)
            state = detect(cfg)
            if state.needs_repo_sync and not skip_sync:
                reasons: list[str] = []
                if state.repo_broken:
                    reasons.append(".repo/ broken")
                if state.manifest_mismatch:
                    reasons.append(f"manifest {state.repo_manifest_include!r} -> {cfg.manifest!r}")
                if state.branch_mismatch:
                    reasons.append(f"branch {state.repo_manifests_branch!r} -> {cfg.repo_branch!r}")
                if state.sha_drift:
                    reasons.append(f"{len(state.sha_drift)} pinned SHA drift")
                if reasons:
                    console.print("[yellow]manifest drift:[/] " + "; ".join(reasons) + " - forcing full re-sync")
                bsp.sync_step(cfg, log, force_init=state.needs_full_reinit)
            else:
                log.step_skip(
                    "repo_sync" if family == "nxp" else "ti_layertool",
                    reason="already synced" if not skip_sync else "user skipped",
                )

            # 2. setup-environment (NXP shells var-setup-release.sh, TI fixes local.conf)
            state = detect(cfg)  # refresh after sync
            if state.needs_setup_env:
                bsp.setup_env_step(cfg, log)
            else:
                log.step_skip("setup_env", reason="bblayers.conf present")

            # 3. apply upstream-bitbake override (NXP and TI). Swaps
            # ``nxp/sources/poky/bitbake`` (NXP) or ``ti/sources/bitbake``
            # (TI) for a symlink to the local upstream clone. No-op when the
            # BSP-bundled tree is missing (pre-bootstrap) or when
            # ``BSPCTL_BITBAKE_OVERRIDE=0``.
            step_override.apply(cfg, log)

            # 4. regenerate kas YAML (always: args may have changed)
            step_kas.regenerate_yaml(cfg, log, bsp=bsp)

        # 5. build
        if dry_run:
            log.step_skip("kas_build", reason="dry-run")
            console.print(f"[green]dry-run complete[/]: would run kas-container with {cfg.kas_yaml} + {overlay_source}")
            return

        rc = step_kas.run_build(
            cfg,
            log,
            kas_yaml=cfg.kas_yaml,
            overlay_source=overlay_source,
        )
        if rc != 0:
            console.print(
                f"[red]kas-container build failed (exit {rc}).[/] Run `bspctl triage {log.run_id}` for details."
            )
            raise typer.Exit(code=rc)
        deploy = cfg.bsp_root / "build" / "tmp" / "deploy" / "images" / cfg.machine
        console.print("[bold green]build succeeded[/]")
        console.print(f"artifacts: {deploy}")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@app.command()
def sync(
    machine: Annotated[str | None, typer.Option("--machine", "-m")] = None,
    distro: Annotated[str | None, typer.Option("--distro", "-d")] = None,
    image: Annotated[str | None, typer.Option("--image", "-i")] = None,
    manifest: Annotated[
        str | None,
        typer.Option(
            "--manifest",
            "-f",
            help="manifest filename (NXP imx-*.xml or TI processor-sdk-*-config_var<N>.txt)",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="branch override; inferred from manifest filename when omitted",
        ),
    ] = None,
    skip_doctor: Annotated[
        bool,
        typer.Option("--skip-doctor", help="Skip the pre-flight diagnosis (not recommended)"),
    ] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Remove <bsp>/build/ before syncing."),
    ] = False,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Run the manifest-driven sync without building.

    Equivalent to the first half of ``bspctl build``: doctor, then
    repo init+sync (NXP) or oe-layertool populate (TI), then var-setup-release
    or local.conf fixup. Useful when you want to refresh ``sources/``
    without kicking off a kas-container build.
    """
    family, bsp = _dispatch_bsp(manifest)
    ws = workspace or _workspace_from_cwd()
    cfg: BuildConfig = resolve(
        workspace=ws,
        bsp_family=family,
        machine=machine,
        distro=distro,
        image=image,
        manifest=manifest,
        repo_branch=branch,
    )

    console.print(f"[bold]::[/] bspctl sync [{family}] manifest={cfg.manifest}")

    if clean:
        _clean_build_dir(cfg)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        if not skip_doctor:
            log.step_start("doctor")
            results = run_all(cfg, bsp)
            _print_diagnosis(results)
            if any_blocking_failure(results):
                log.step_fail("doctor", reason="blocking failure")
                raise typer.Exit(code=2)
            log.step_ok("doctor", checks=len(results))

        state = detect(cfg)
        if state.needs_repo_sync:
            reasons: list[str] = []
            if state.repo_broken:
                reasons.append(".repo/ broken")
            if state.manifest_mismatch:
                reasons.append(f"manifest {state.repo_manifest_include!r} -> {cfg.manifest!r}")
            if state.branch_mismatch:
                reasons.append(f"branch {state.repo_manifests_branch!r} -> {cfg.repo_branch!r}")
            if state.sha_drift:
                reasons.append(f"{len(state.sha_drift)} pinned SHA drift")
            if reasons:
                console.print("[yellow]manifest drift:[/] " + "; ".join(reasons) + " - forcing full re-sync")
            bsp.sync_step(cfg, log, force_init=state.needs_full_reinit)
        else:
            log.step_skip(
                "repo_sync" if family == "nxp" else "ti_layertool",
                reason="already synced",
            )

        state = detect(cfg)
        if state.needs_setup_env:
            bsp.setup_env_step(cfg, log)
        else:
            log.step_skip("setup_env", reason="bblayers.conf present")

    console.print("[bold green]sync complete[/]")


# ---------------------------------------------------------------------------
# triage
# ---------------------------------------------------------------------------


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
        for entry in runs_dir.iterdir():
            if entry.is_dir():
                candidates.append((entry, label))

    if not candidates:
        return None

    if run_id is None:
        candidates.sort(key=lambda pair: pair[0].name, reverse=True)
        return candidates[0]

    for run_dir, label in candidates:
        if run_dir.name == run_id:
            return (run_dir, label)
    return None


@app.command()
def triage(
    run_id: Annotated[str | None, typer.Argument(help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted.")] = None,
    kas_yaml: Annotated[
        Path | None,
        typer.Option(
            "--kas-yaml",
            "-k",
            help="kas YAML for a BYO build; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Surface the last failed step of the named run (or the most recent).

    Without ``--kas-yaml`` searches both ``nxp/build/runs/`` and
    ``ti/build/runs/`` under the workspace. Pass ``--kas-yaml my.yml``
    for a BYO build whose runs live next to the YAML
    (``<yaml-parent>/build/runs/``); the BSP family is inferred from
    the run directory's location and reported as ``generic`` for
    non-Variscite BYO YAMLs.
    """
    if kas_yaml is not None:
        runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = [
            (kas_yaml.resolve().parent / "build" / "runs", "generic"),
        ]
        report_root = kas_yaml.resolve().parent
        not_found_label = f"{runs_dirs[0][0]}"
    else:
        ws = workspace or _workspace_from_cwd()
        runs_dirs = [
            (ws / "nxp" / "build" / "runs", "nxp"),
            (ws / "ti" / "build" / "runs", "ti"),
        ]
        report_root = ws
        not_found_label = "nxp/build/runs/ or ti/build/runs/"

    found = _find_run(runs_dirs, run_id)
    if found is None:
        if run_id:
            console.print(f"[red]Run {run_id} not found under {not_found_label}[/]")
        else:
            console.print(f"[yellow]No runs found under {not_found_label}.[/]")
        raise typer.Exit(code=1)

    run_dir, _label = found
    report = analyse(run_dir, report_root)
    console.print(f"[bold]::[/] triage {run_dir.name}")
    if report.failing_step:
        console.print(f"[red]✗[/] step [bold]{report.failing_step}[/] failed: {report.fail_reason}")
    else:
        console.print("[green]no step_fail events found[/]")

    if report.kas_log_tail:
        console.print("[dim]kas.log (tail):[/]")
        for line in report.kas_log_tail:
            sys.stdout.write(f"  {line.rstrip()}\n")
        sys.stdout.flush()
    if report.recipe_log:
        console.print(f"[bold]bitbake recipe log:[/] {report.recipe_log}")
        if report.recipe_log_tail:
            console.print(f"[dim]{report.recipe_log.name} (tail):[/]")
            for line in report.recipe_log_tail:
                sys.stdout.write(f"  {line.rstrip()}\n")
            sys.stdout.flush()
    if report.suggestions:
        console.print("[cyan]suggestions:[/]")
        for s in report.suggestions:
            console.print(f"  - {s}")


# ---------------------------------------------------------------------------
# shell / gen-kas / clean
# ---------------------------------------------------------------------------


@app.command()
def shell(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; when supplied, BSP family is inferred from it instead of --manifest.",
        ),
    ] = None,
    command: Annotated[
        str | None,
        typer.Option(
            "--command",
            "-c",
            help="Run command inside kas-container shell instead of dropping into an interactive shell",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    extra: Annotated[
        list[str] | None,
        typer.Argument(help="Extra args passed through to kas-container shell"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
    host_mode: Annotated[
        bool,
        typer.Option(
            "--host",
            help="Bypass kas-container and run plain `kas shell` directly on the host.",
        ),
    ] = False,
) -> None:
    """Drop into a `kas-container shell` for this project, or run a single command via -c.

    Pass ``--host`` to invoke plain ``kas shell`` directly on the host
    (no kas-container wrapper, no Docker). Requires host bitbake build
    prereqs.
    """
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        manifest=manifest,
        host_mode=host_mode,
        kas_yaml=kas_yaml,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        rc = step_kas.run_shell(
            cfg,
            log,
            list(extra or []),
            command=command,
            kas_yaml=cfg.kas_yaml,
            overlay_source=overlay_source,
        )
    raise typer.Exit(code=rc)


@app.command()
def run(
    kas_yaml: Annotated[
        Path,
        typer.Argument(
            exists=False,
            help="kas YAML identifying the target machine (e.g. kas/machine/qemux86-64.yml).",
        ),
    ],
    swtpm: Annotated[
        bool,
        typer.Option(
            "--swtpm/--no-swtpm",
            help="Run with software TPM via swtpm daemon (default: enabled).",
        ),
    ] = True,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Boot an avocado-os image in QEMU from the build directory.

    Requires a completed build with stone provisioning. Run
    ``bspctl build kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml``
    first to produce the bootable disk image.

    Only supported for meta-avocado builds (avocado-qemux86-64, avocado-qemuarm64).
    """
    family, _ = _dispatch_from_yaml(kas_yaml)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        kas_yaml=kas_yaml,
    )

    if not cfg.is_meta_avocado:
        console.print("[red]bspctl run currently only supports meta-avocado builds[/]")
        raise typer.Exit(code=2)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        try:
            rc = step_run.run_qemu(cfg, log, swtpm=swtpm, kas_yaml=kas_yaml)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=2) from None
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from None
    raise typer.Exit(code=rc)


@app.command("gen-kas")
def gen_kas(
    machine: Annotated[str | None, typer.Option("--machine", "-m")] = None,
    distro: Annotated[str | None, typer.Option("--distro", "-d")] = None,
    image: Annotated[str | None, typer.Option("--image", "-i")] = None,
    manifest: Annotated[str | None, typer.Option("--manifest", "-f")] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="branch override; inferred from manifest when omitted"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output path; defaults to <bsp_root>/kas-<bsp>.yml"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Regenerate the topology-only kas YAML from current inputs.

    Output is the manifest -> repos topology only. The Variscite tuning
    block and the meta-varis-overrides repo entry live in the static
    overlay at ``overlays/bspctl-tuning-<bsp>.yml`` and are layered in
    by ``bspctl build`` at run time.

    Default output path is ``<bsp_root>/kas-<bsp>.yml``; use
    ``-o my-build.yml`` to write somewhere else.
    """
    family, bsp = _dispatch_bsp(manifest)
    ws = workspace or _workspace_from_cwd()
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        machine=machine,
        distro=distro,
        image=image,
        manifest=manifest,
        repo_branch=branch,
    )
    out_path = output.resolve() if output is not None else cfg.default_kas_yaml
    opts = KasGenOptions(
        manifest=cfg.manifest_path,
        bblayers=cfg.bblayers_conf if cfg.bblayers_conf.is_file() else None,
        machine=cfg.machine,
        distro=cfg.distro,
        target=cfg.image,
        output=out_path,
        workspace=cfg.workspace,
        template=bsp.kas_template,
        skip_manifest=(bsp.manifest_kind != "repo-xml"),
    )
    write_yaml(opts)
    console.print(f"[green]wrote[/] {out_path}")


# ---------------------------------------------------------------------------
# bitbake-override
# ---------------------------------------------------------------------------


@app.command("bitbake-override")
def bitbake_override_cmd(
    apply_flag: Annotated[
        bool,
        typer.Option("--apply", help="Apply the override (default action)."),
    ] = False,
    revert_flag: Annotated[
        bool,
        typer.Option("--revert", help="Remove the symlink; next build's repo sync restores the BSP bitbake."),
    ] = False,
    status_flag: Annotated[
        bool,
        typer.Option("--status", help="Print current override state and exit."),
    ] = False,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Override branch in the upstream bitbake repo. "
            "Defaults to br-<major>.<minor> auto-detected from the BSP's bundled bitbake.",
        ),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Path to the upstream bitbake source repo. "
            "Defaults to ~/repos/personal/yocto/bitbake or BSPCTL_BITBAKE_OVERRIDE_REPO.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option(
            "--manifest",
            "-f",
            help="Manifest filename used to dispatch BSP family. Defaults to BSPCTL_MANIFEST or the NXP default.",
        ),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Swap the BSP-bundled bitbake for a symlink to a local upstream checkout.

    Default action with no flags is --status. Use --apply to swap, --revert
    to remove the symlink (next ``bspctl build`` re-syncs to restore the BSP
    bitbake). The override is auto-applied as part of ``bspctl build``.

    The path swapped depends on the dispatched BSP family: NXP swaps
    ``nxp/sources/poky/bitbake``; TI swaps ``ti/sources/bitbake``. Pass
    ``--manifest`` (or set ``BSPCTL_MANIFEST``) to target TI; without it
    the command defaults to the NXP family.
    """
    selected = sum(1 for f in (apply_flag, revert_flag, status_flag) if f)
    if selected > 1:
        console.print("[red]choose at most one of --apply / --revert / --status[/]")
        raise typer.Exit(code=2)

    ws = workspace or _workspace_from_cwd()
    family, _bsp = _dispatch_bsp(manifest)
    cfg = resolve(workspace=ws, bsp_family=family, manifest=manifest)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    if revert_flag:
        with RunLogger(runs_dir=cfg.runs_dir) as log:
            step_override.revert(cfg, log)
        st = step_override.status(cfg)
        console.print(f"[yellow]reverted[/]: {st.detail}")
        return

    if apply_flag:
        with RunLogger(runs_dir=cfg.runs_dir) as log:
            try:
                result = step_override.apply(cfg, log, branch=branch, repo_path=repo)
            except RuntimeError as exc:
                console.print(f"[red]bitbake-override failed[/]: {exc}")
                raise typer.Exit(code=2) from exc
        _print_override_status(result)
        return

    # default: --status
    st = step_override.status(cfg)
    _print_override_status(st)


def _print_override_status(st) -> None:
    state_colour = {
        "active": "green",
        "stale": "yellow",
        "disabled": "dim",
        "missing": "dim",
    }.get(st.state, "white")
    bits: list[str] = [f"[{state_colour}]{st.state}[/]"]
    if st.branch:
        bits.append(f"branch={st.branch}")
    if st.sha:
        bits.append(f"sha={st.sha}")
    if st.upstream_version:
        bits.append(f"upstream={st.upstream_version}")
    if st.bsp_version:
        bits.append(f"bsp={st.bsp_version}")
    bits.append(f"({st.detail})")
    console.print("bitbake-override: " + " ".join(bits))


# ---------------------------------------------------------------------------
# stress-parse
# ---------------------------------------------------------------------------


@app.command("stress-parse")
def stress_parse(
    runs: Annotated[
        int,
        typer.Option("--runs", "-n", help="Number of bitbake -p iterations to run."),
    ] = 10,
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="bitbake parse target. Default 'world' parses every recipe in the layer graph.",
        ),
    ] = "world",
    parse_threads: Annotated[
        int | None,
        typer.Option(
            "--parse-threads",
            help="Override BB_NUMBER_PARSE_THREADS for each iteration. "
            "Use 1 to confirm the serial baseline; raise above nproc to amplify the race.",
        ),
    ] = None,
    machine: Annotated[str | None, typer.Option("--machine", "-m")] = None,
    image: Annotated[str | None, typer.Option("--image", "-i")] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Branch override; inferred from manifest when omitted."),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    host_mode: Annotated[
        bool,
        typer.Option(
            "--host",
            help="Bypass kas-container and run plain `kas shell` directly on the host. "
            "Used by the VARIS-18 Round 8 probe to rule out kas-container/Docker as the "
            "parser-fork-race environment. Requires host bitbake build prereqs.",
        ),
    ] = False,
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Free-form tag persisted into summary.json for cross-run aggregation. "
            "Used by the patch-ablation matrix and CPython version sweep to group runs.",
        ),
    ] = None,
    python: Annotated[
        Path | None,
        typer.Option(
            "--python",
            help="Override which Python bitbake re-execs into. Sets BB_PYTHON3 and "
            "leads PATH with the interpreter's bin dir, so stress-parse can validate "
            "a locally-built CPython (e.g. one carrying the VARIS-19 obmalloc fork-"
            "safety patch) without reinstalling varis under that interpreter. Pass "
            "the absolute path to a python binary; defaults to varis's own interpreter.",
        ),
    ] = None,
) -> None:
    """Stress-test the bitbake parser fork-race fix with N parse-only iterations.

    Loops ``bitbake -p <target>`` inside ``kas-container`` (or directly on
    the host when ``--host`` is given) and scans each iteration's output
    for the canonical fork-race signatures. Writes per-iteration logs
    and a ``summary.json`` under
    ``<bsp>/build/runs/<run-id>/stress-parse/``. Exits non-zero if any
    iteration tripped a signature.

    Assumes the workspace is already synced (run ``bspctl build`` once
    first); applies the bitbake override and regenerates the kas YAML
    before looping. Skips the doctor pre-flight - the user opts into
    stress-parse explicitly.
    """
    if runs < 1:
        console.print("[red]--runs must be >= 1[/]")
        raise typer.Exit(code=2)

    python_executable: Path | None = None
    if python is not None:
        python_executable = python.expanduser().resolve()
        if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
            console.print(f"[red]--python: not an executable file: {python_executable}[/]")
            raise typer.Exit(code=2)

    family, bsp = _dispatch_bsp(manifest)
    ws = workspace or _workspace_from_cwd()
    cfg: BuildConfig = resolve(
        workspace=ws,
        bsp_family=family,
        machine=machine,
        image=image,
        manifest=manifest,
        repo_branch=branch,
        host_mode=host_mode,
    )
    overlay_source = _overlay_for(bsp)

    console.print(
        f"[bold]::[/] bspctl stress-parse [{family}] {cfg.machine} / {cfg.image} "
        f"target={target} runs={runs}"
        + (f" parse-threads={parse_threads}" if parse_threads is not None else "")
        + (" [host-mode]" if host_mode else "")
        + (f" label={label!r}" if label else "")
        + (f" python={python_executable}" if python_executable is not None else "")
    )

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        step_override.apply(cfg, log)
        step_kas.regenerate_yaml(cfg, log, bsp=bsp)
        summary = step_stress_parse.run(
            cfg,
            log,
            bsp=bsp,
            overlay_source=overlay_source,
            runs=runs,
            target=target,
            parse_threads=parse_threads,
            label=label,
            python_executable=python_executable,
        )

    table = Table(title=f"stress-parse summary ({family})", show_edge=False)
    table.add_column("metric", no_wrap=True)
    table.add_column("value")
    table.add_row("runs", str(summary["runs"]))
    table.add_row("passed", f"[green]{summary['passed']}[/]")
    fail_colour = "red" if summary["failed"] else "dim"
    table.add_row("failed", f"[{fail_colour}]{summary['failed']}[/]")
    if summary["elapsed_seconds"]:
        avg = sum(summary["elapsed_seconds"]) / len(summary["elapsed_seconds"])
        table.add_row("avg seconds", f"{avg:.1f}")
    if summary["override"]:
        ov = summary["override"]
        table.add_row(
            "override",
            f"{ov.get('state')} branch={ov.get('branch')} sha={ov.get('sha')}",
        )
    console.print(table)

    if summary["failed"] > 0:
        for sig in summary["failure_signatures"][:10]:
            console.print(f"[red]run {sig['run']}[/] [{sig['pattern']}] {sig['match']}")
        if len(summary["failure_signatures"]) > 10:
            console.print(f"[dim]... {len(summary['failure_signatures']) - 10} more matches truncated[/]")
        raise typer.Exit(code=1)


@app.command()
def clean(
    all: Annotated[bool, typer.Option("--all", help="Also remove the generated kas YAML")] = False,
    bsp: Annotated[
        str | None,
        typer.Option("--bsp", help="BSP family to clean: 'nxp' or 'ti'. Auto-detected from cwd if omitted."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename (back-compat alias for --bsp)"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Remove the BSP-specific build/ directory. Use --all to also drop the kas YAML."""
    ws = workspace or _workspace_from_cwd()

    family: Literal["nxp", "ti"] | None = None
    if bsp is not None:
        if bsp not in ("nxp", "ti"):
            console.print(f"[red]invalid --bsp value[/]: {bsp!r} (expected 'nxp' or 'ti')")
            raise typer.Exit(code=2)
        family = bsp  # type: ignore[assignment]
    elif manifest is not None:
        family, _bsp_model = _dispatch_bsp(manifest)
    else:
        family = _bsp_from_cwd(ws)
        if family is None:
            console.print("[red]could not auto-detect BSP from cwd. Pass --bsp nxp|ti or --manifest <file>.[/]")
            raise typer.Exit(code=2)

    cfg = resolve(workspace=ws, bsp_family=family)
    _clean_build_dir(cfg)
    if all and cfg.kas_yaml.exists():
        cfg.kas_yaml.unlink()
        console.print(f"[green]removed[/] {cfg.kas_yaml}")


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


_LOG_FILES: dict[str, str] = {
    "kas": "kas.log",
    "console": "console.log",
    "events": "events.jsonl",
}


def _tail_follow(path: Path, history_lines: int = 40) -> None:
    """Pure-Python `tail -f`: print the last N lines, then stream new content.

    Seeking straight to EOF hides everything already written to the log,
    so if the build is between writes when the user opens the tail, the
    screen stays blank. Emit a chunk of recent history first (matches
    `tail -f` default behavior) so `bspctl log` is useful mid-run.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
        for line in lines[-history_lines:]:
            sys.stdout.write(line)
        sys.stdout.flush()
        # `readlines()` leaves the cursor at EOF, so the follow loop
        # below picks up only genuinely new content from here on.
        while True:
            line = fh.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.2)


@app.command("log")
def log_cmd(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted."),
    ] = None,
    which: Annotated[
        str,
        typer.Option("--which", help="Which log to follow: kas, console, or events."),
    ] = "kas",
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Tail the latest bspctl run's kas.log live. Use --run for a specific run, --which to pick a different log file.

    Pass a positional kas YAML for BYO builds (``bspctl log my.yml``);
    runs live next to the YAML under ``<yaml-parent>/build/runs/`` and
    the workspace lookup is skipped.
    """
    if which not in _LOG_FILES:
        console.print(f"[red]invalid --which value[/]: {which!r} (expected one of: kas, console, events)")
        raise typer.Exit(code=2)

    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, _bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(workspace=ws, bsp_family=family, manifest=manifest, kas_yaml=kas_yaml)
    runs_dir = cfg.runs_dir
    if not runs_dir.is_dir():
        console.print("[red]no runs yet[/]; start one with `bspctl build`")
        raise typer.Exit(code=1)

    run_dirs = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    if not run_dirs:
        console.print("[red]no runs yet[/]; start one with `bspctl build`")
        raise typer.Exit(code=1)

    if run is None:
        run_dir = run_dirs[-1]
    else:
        run_dir = runs_dir / run
        if not run_dir.is_dir():
            console.print(f"[red]run directory not found[/]: {run_dir}")
            raise typer.Exit(code=1)

    log_name = _LOG_FILES[which]
    target = run_dir / log_name
    if not target.is_file():
        if which == "kas":
            fallback = run_dir / _LOG_FILES["console"]
            if fallback.is_file():
                console.print(
                    f"[yellow]note:[/] {log_name} not present yet (build hasn't reached "
                    f"kas_build); falling back to {fallback.name}"
                )
                target = fallback
            else:
                console.print(f"[red]no kas.log or console.log in[/] {run_dir}")
                raise typer.Exit(code=1)
        else:
            console.print(f"[red]log file not found[/]: {target}")
            raise typer.Exit(code=1)

    console.print(f"[dim]following[/] {target}")
    try:
        _tail_follow(target)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
