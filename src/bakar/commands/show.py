"""bakar show subcommand - resolved build picture from local data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _overlay_for,
    _resolve_workspace,
    _tuning_extra_overlays,
)
from bakar.config import BSPSpec, resolve
from bakar.layers import collect_layer_hashes, discover_source_repos


def _build_command_str(cfg, overlay_source: Path, extra_overlays: list[Path]) -> str:  # type: ignore[no-untyped-def]
    """Return the exact kas-container invocation a build would run.

    Mirrors the logic in ``dry_run_preview_lines`` without importing
    internal helpers from ``kas_build`` that materialise overlay files -
    those would create filesystem side effects. For the show command we
    only need a human-readable preview, so we compose the string directly
    from the resolved config fields.
    """
    exe = "kas" if cfg.host_mode else "kas-container"
    ccache_host = cfg.effective_ccache_dir
    runtime_flag = f"-v {ccache_host}:/work/ccache:rw"
    if cfg.use_hashequiv:
        runtime_flag += " --add-host=host.docker.internal:host-gateway"

    # kas_yaml relative to bsp_root when not overridden; full path otherwise.
    kas_yaml_str = str(cfg.kas_yaml)
    overlay_name = f".bakar/overlays/{overlay_source.name}"
    parts = [f"{kas_yaml_str}:{overlay_name}", *[f".bakar/overlays/{p.name}" for p in extra_overlays]]
    kas_arg = ":".join(parts)

    if cfg.host_mode:
        cmd_parts = [exe, "build", kas_arg]
    else:
        cmd_parts = [exe, "--runtime-args", runtime_flag, "build", kas_arg]
    return " ".join(cmd_parts)


@app.command("show")
def show(
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
        typer.Option("--json", help="Emit a JSON document with keys config, overlays, layers, sources, command"),
    ] = False,
    fmt: Annotated[
        str,
        typer.Option("--format", help="Output format: text (default) or md"),
    ] = "text",
) -> None:
    """Print the resolved build picture from local data only (no kas-container).

    Shows five sections: Config (machine, distro, image, BSP family, container
    image, DL_DIR, SSTATE_DIR), Overlays, Layers, Sources, and the exact
    kas-container command a build would run.

    Exits 0 even when the workspace has not been built yet - the Layers and
    Sources sections will appear empty. Exits 2 when no workspace can be found.
    """
    family, bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    extra_overlays = _tuning_extra_overlays(cfg)

    # Collect local data - none of these make container calls.
    layer_hashes = collect_layer_hashes(cfg)
    source_repos = discover_source_repos(cfg)

    # --- Config section ---
    config_data: dict[str, str] = {
        "machine": cfg.machine,
        "distro": cfg.distro,
        "image": cfg.image,
        "bsp_family": cfg.bsp_family,
        "container_image": cfg.container_image,
        "dl_dir": cfg.dl_dir or "",
        "sstate_dir": cfg.sstate_dir or "",
    }

    # --- Overlays section ---
    overlays_list = [overlay_source.name, *[p.name for p in extra_overlays]]

    # --- Layers section ---
    layers_list = [
        {"repo": lh.repo, "short_hash": lh.short_hash, "branch": lh.branch, "version": lh.version or ""}
        for lh in layer_hashes
    ]

    # --- Sources section ---
    sources_list = [{"name": name, "path": str(path)} for name, path in source_repos]

    # --- Command section ---
    command_str = _build_command_str(cfg, overlay_source, extra_overlays)

    if output_json:
        doc = {
            "config": config_data,
            "overlays": overlays_list,
            "layers": layers_list,
            "sources": sources_list,
            "command": command_str,
        }
        typer.echo(json.dumps(doc, indent=2))
        return

    use_md = fmt == "md"
    _heading = (lambda t: f"## {t}") if use_md else (lambda t: f"{t}:")

    # Config
    console.print(_heading("Config"), highlight=False)
    for key, val in config_data.items():
        display = val if val else "(not set)"
        console.print(f"  {key}: {display}", highlight=False)

    # Overlays
    console.print(_heading("Overlays"), highlight=False)
    for name in overlays_list:
        console.print(f"  {name}", highlight=False)

    # Layers
    console.print(_heading("Layers"), highlight=False)
    if layer_hashes:
        width = max(len(lh.repo) for lh in layer_hashes)
        for lh in layer_hashes:
            branch = f"  ({lh.branch})" if lh.branch else ""
            ver = f"  {lh.version}" if lh.version else ""
            console.print(f"  {lh.repo:<{width}}  {lh.short_hash}{branch}{ver}", highlight=False)
    else:
        console.print("  (none - run `bakar build` or `bakar sync` first)", highlight=False)

    # Sources
    console.print(_heading("Sources"), highlight=False)
    if source_repos:
        for name, path in source_repos:
            console.print(f"  {name}: {path}", highlight=False)
    else:
        console.print("  (none - run `bakar sync` first)", highlight=False)

    # Command
    console.print(_heading("Command"), highlight=False)
    console.print(f"  {command_str}", highlight=False)
