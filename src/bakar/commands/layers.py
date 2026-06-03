"""bakar layers sub-app - display synced layer git state and per-layer details."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _normalize_dispatch,
    _overlay_for,
    _print_layer_hashes,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.inspect_parse import parse_getvar_value, parse_layer_conf
from bakar.kas import parse_bblayers
from bakar.layers import _parse_bbsetup_layer_repos, collect_layer_hashes
from bakar.observability import RunLogger
from bakar.steps import kas_build as step_kas
from bakar.steps.kas_build import KasBuildContext

# ---------------------------------------------------------------------------
# Sub-app
# ---------------------------------------------------------------------------

layers_app = typer.Typer(
    name="layers",
    help="Display synced layer git state and per-layer details.",
    no_args_is_help=False,
    invoke_without_command=True,
)

app.add_typer(layers_app, name="layers")

# ---------------------------------------------------------------------------
# Shared option helpers (avoids B008 by wrapping in lambdas or using defaults
# on the Annotated type - kept as type aliases here for readability)
# ---------------------------------------------------------------------------


def _common_options(
    kas_yaml: Path | None,
    manifest: str | None,
    workspace: Path | None,
) -> tuple:
    """Validate mutual-exclusion, normalize dispatch, and resolve (family, bsp, ws, cfg)."""
    family, bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    return family, bsp, ws, cfg


# ---------------------------------------------------------------------------
# Bare ``bakar layers`` - preserved git-hash + branch listing
# ---------------------------------------------------------------------------


@layers_app.callback(invoke_without_command=True)
def layers(
    ctx: typer.Context,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Print each synced layer's repo name, git short-hash, and branch.

    Read-only: collects layer git state once and never triggers a build,
    sync, or any workspace write.
    """
    if ctx.invoked_subcommand is not None:
        # A sub-verb was given (inspect / status) - let the subcommand run.
        return

    family, _bsp, _kas_yaml, manifest = _normalize_dispatch(None, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=_kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=_kas_yaml,
        user_config=_state._USER_CONFIG,
    )

    hashes = collect_layer_hashes(cfg)
    if not hashes:
        console.print("no layers yet; run `bakar build` or `bakar sync` first")
        raise typer.Exit(code=0)

    _print_layer_hashes(cfg, hashes=hashes)


# ---------------------------------------------------------------------------
# ``bakar layers inspect`` - per-layer report
# ---------------------------------------------------------------------------


@layers_app.command("inspect")
def layers_inspect(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO); resolves the workspace next to it.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable text"),
    ] = False,
) -> None:
    """Print a per-layer report: name, path, priority, compat releases, version.

    Reads layer.conf from the local workspace to extract BBFILE_PRIORITY,
    LAYERSERIES_COMPAT, and LAYERVERSION, then runs ``bitbake-layers
    show-layers`` inside kas-container for the canonical priority and
    what each layer provides.

    Container-backed: requires a synced workspace.
    """
    _family, bsp, _ws, cfg = _common_options(kas_yaml, manifest, workspace)

    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    # Build the per-layer data from local layer.conf files
    # bblayers.conf lists paths like /work/sources/<repo>/meta-<layer>
    # or ${TOPDIR}/../layers/<repo>/<layer>. We parse them to find layer.conf.
    layer_records: list[dict] = []

    # Collect layer paths from bblayers.conf
    layer_paths = _collect_bblayer_paths(cfg)

    for layer_name, layer_path in layer_paths:
        conf_path = layer_path / "conf" / "layer.conf"
        conf_data: dict = {}
        if conf_path.is_file():
            try:
                conf_data = parse_layer_conf(conf_path.read_text())
            except OSError:
                pass

        record: dict = {
            "name": layer_name,
            "path": str(layer_path),
            "priority": conf_data.get("BBFILE_PRIORITY", ""),
            "compat": conf_data.get("LAYERSERIES_COMPAT", ""),
            "version": conf_data.get("LAYERVERSION", ""),
            "provides": _detect_provides(layer_path),
        }
        layer_records.append(record)

    # Run bitbake-layers show-layers inside the container for authoritative data
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)
        capture_path = cfg.runs_dir / "layers_inspect.txt"
        rc = step_kas.run_shell_capture(
            kas_ctx,
            "bitbake-layers show-layers",
            capture_path,
            step="layers_inspect",
        )

    if rc == 0 and capture_path.is_file():
        _merge_show_layers(capture_path.read_text(), layer_records)

    if output_json:
        console.print(json.dumps(layer_records, indent=2))
        return

    if not layer_records:
        console.print("no layers found; run `bakar build` or `bakar sync` first")
        raise typer.Exit(code=0)

    for rec in layer_records:
        console.print(f"[bold]{rec['name']}[/]")
        if rec["path"]:
            console.print(f"  path:     {rec['path']}", highlight=False)
        if rec["priority"]:
            console.print(f"  priority: {rec['priority']}", highlight=False)
        if rec["compat"]:
            console.print(f"  compat:   {rec['compat']}", highlight=False)
        if rec["version"]:
            console.print(f"  version:  {rec['version']}", highlight=False)
        if rec.get("provides"):
            console.print(f"  provides: {rec['provides']}", highlight=False)
        console.print()


# ---------------------------------------------------------------------------
# ``bakar layers status`` - project-level summary
# ---------------------------------------------------------------------------

_STATUS_VARS = [
    "MACHINE",
    "DISTRO",
    "DISTRO_CODENAME",
    "BB_NUMBER_THREADS",
    "PARALLEL_MAKE",
    "SOURCE_MIRROR_URL",
    "SSTATE_MIRRORS",
    "BB_HASHSERV",
]


@layers_app.command("status")
def layers_status(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO); resolves the workspace next to it.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable text"),
    ] = False,
) -> None:
    """Print a project-level build summary.

    Queries effective MACHINE, DISTRO, DISTRO_CODENAME, BB_NUMBER_THREADS,
    PARALLEL_MAKE, SOURCE_MIRROR_URL, SSTATE_MIRRORS, and the hashserv URL
    via ``bitbake-getvar`` inside kas-container.

    Container-backed: requires a synced workspace.
    """
    _family, bsp, _ws, cfg = _common_options(kas_yaml, manifest, workspace)

    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    var_values: dict[str, str] = {}

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)
        for var in _STATUS_VARS:
            capture_path = cfg.runs_dir / f"layers_status_{var}.txt"
            rc = step_kas.run_shell_capture(
                kas_ctx,
                f"bitbake-getvar {var}",
                capture_path,
                step=f"layers_status_{var}",
            )
            if rc == 0 and capture_path.is_file():
                value = parse_getvar_value(capture_path.read_text(), var)
                if value:
                    var_values[var] = value

    if output_json:
        summary = _build_status_summary(var_values)
        console.print(json.dumps(summary, indent=2))
        return

    _print_status(var_values)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_provides(layer_path: Path) -> str:
    """Return a summary of what a layer provides by scanning its conf/ dirs.

    Checks ``conf/machine/*.conf`` and ``conf/distro/*.conf`` under the
    layer path. Returns a human-readable string like "machines: imx8mp,
    imx6; distros: fsl-imx-xwayland" or empty string when nothing is found.
    """
    parts: list[str] = []
    try:
        machines = sorted(p.stem for p in (layer_path / "conf" / "machine").iterdir() if p.suffix == ".conf")
        if machines:
            parts.append("machines: " + ", ".join(machines))
    except OSError:
        pass
    try:
        distros = sorted(p.stem for p in (layer_path / "conf" / "distro").iterdir() if p.suffix == ".conf")
        if distros:
            parts.append("distros: " + ", ".join(distros))
    except OSError:
        pass
    return "; ".join(parts)


def _collect_bblayer_paths(cfg) -> list[tuple[str, Path]]:
    """Return (layer_name, layer_path) pairs from bblayers.conf.

    Tries to resolve paths from the local workspace using the same
    strategies as collect_layer_hashes (bbsetup layers/ and sources/).
    Falls back to skipping unresolvable paths silently.
    """
    if not cfg.bblayers_conf.is_file():
        return []

    results: list[tuple[str, Path]] = []

    # bbsetup strategy: ${TOPDIR}/../layers/<repo>/meta-<layer>
    for repo in _parse_bbsetup_layer_repos(cfg.bblayers_conf):
        repo_path = cfg.bsp_root / "layers" / repo
        if repo_path.is_dir():
            # Find the layer subdirectory (meta-<repo> or the repo itself)
            results.extend(
                (child.name, child)
                for child in sorted(repo_path.iterdir())
                if child.is_dir() and (child / "conf" / "layer.conf").is_file()
            )
            # Also check the repo root itself
            if (repo_path / "conf" / "layer.conf").is_file():
                results.append((repo, repo_path))

    if results:
        return results

    # NXP/TI sources/ strategy - parse_bblayers returns {repo: {layer, ...}}
    for repo in parse_bblayers(cfg.bblayers_conf):
        repo_path = cfg.bsp_root / "sources" / repo
        if repo_path.is_dir():
            results.extend(
                (child.name, child)
                for child in sorted(repo_path.iterdir())
                if child.is_dir() and (child / "conf" / "layer.conf").is_file()
            )
            if (repo_path / "conf" / "layer.conf").is_file():
                results.append((repo, repo_path))

    return results


def _merge_show_layers(text: str, records: list[dict]) -> None:
    """Parse ``bitbake-layers show-layers`` output and enrich *records* in place.

    The output format is::

        layer                 path                                      priority
        =======================================================================
        meta                  /work/sources/poky/meta                   5
        ...

    Matches by layer name and fills in ``priority`` (container-authoritative)
    and ``provides`` (from the path column, if available).
    """

    name_map = {r["name"]: r for r in records}
    for line in text.splitlines():
        # Skip header and separator lines
        stripped = line.strip()
        # Skip separator lines (===...) and the "layer  path  priority" header line.
        # Use "layer " (with space) so layer names like "layer-base" are NOT skipped.
        if not stripped or stripped.startswith(("=", "layer ")):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        layer_name = parts[0]
        # priority is the last token (an integer)
        try:
            priority = str(int(parts[-1]))
        except ValueError:
            continue
        if layer_name in name_map:
            # Container-authoritative priority wins over local layer.conf parse
            name_map[layer_name]["priority"] = priority
        else:
            # Layer from container not in our local list - add it
            path_str = parts[1] if len(parts) >= 2 else ""
            new_record = {
                "name": layer_name,
                "path": path_str,
                "priority": priority,
                "compat": "",
                "version": "",
            }
            records.append(new_record)
            name_map[layer_name] = new_record


def _build_status_summary(var_values: dict[str, str]) -> dict:
    """Build the structured summary dict for --json output."""
    return {
        "machine": var_values.get("MACHINE", ""),
        "distro": var_values.get("DISTRO", ""),
        "distro_codename": var_values.get("DISTRO_CODENAME", ""),
        "bb_number_threads": var_values.get("BB_NUMBER_THREADS", ""),
        "parallel_make": var_values.get("PARALLEL_MAKE", ""),
        "source_mirror_url": var_values.get("SOURCE_MIRROR_URL", "") or None,
        "sstate_mirrors_configured": bool(var_values.get("SSTATE_MIRRORS", "").strip()),
        "hashserv_url": var_values.get("BB_HASHSERV", "") or None,
    }


def _print_status(var_values: dict[str, str]) -> None:
    """Print the project-level status in human-readable form."""
    machine = var_values.get("MACHINE", "(not set)")
    distro = var_values.get("DISTRO", "(not set)")
    codename = var_values.get("DISTRO_CODENAME", "")
    threads = var_values.get("BB_NUMBER_THREADS", "")
    parallel = var_values.get("PARALLEL_MAKE", "")
    mirror_url = var_values.get("SOURCE_MIRROR_URL", "")
    sstate = var_values.get("SSTATE_MIRRORS", "")
    hashserv = var_values.get("BB_HASHSERV", "")

    console.print("status:", highlight=False)
    console.print(f"  MACHINE:           {machine}", highlight=False)
    console.print(f"  DISTRO:            {distro}", highlight=False)
    if codename:
        console.print(f"  DISTRO_CODENAME:   {codename}", highlight=False)
    if threads:
        console.print(f"  BB_NUMBER_THREADS: {threads}", highlight=False)
    if parallel:
        console.print(f"  PARALLEL_MAKE:     {parallel}", highlight=False)
    console.print(f"  SOURCE_MIRROR_URL: {'set' if mirror_url.strip() else 'not set'}", highlight=False)
    console.print(f"  SSTATE_MIRRORS:    {'configured' if sstate.strip() else 'not configured'}", highlight=False)
    if hashserv:
        console.print(f"  hashserv:          {hashserv}", highlight=False)
    else:
        console.print("  hashserv:          not configured", highlight=False)
