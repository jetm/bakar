"""bakar gen-kas subcommand - regenerate kas YAML without building."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _bbsetup_workspace,
    _dispatch_bsp,
    _workspace_from_cwd,
)
from bakar.config import BSPSpec, resolve
from bakar.kas import KasGenOptions, write_bbsetup_yaml, write_yaml


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
    workspace: WorkspaceOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Print the resolved output and source paths, then exit without writing"),
    ] = False,
) -> None:
    """Regenerate the topology-only kas YAML from current inputs.

    Output is the manifest -> repos topology only. The BSP tuning
    block and the meta-varis-overrides repo entry live in the static
    overlay at ``overlays/bakar-tuning-<bsp>.yml`` and are layered in
    by ``bakar build`` at run time.

    Default output path is ``<bsp_root>/kas-<bsp>.yml``; use
    ``-o my-build.yml`` to write somewhere else.

    For a bitbake-setup workspace (``-w <setup-dir>``) the kas YAML is
    translated from ``config/config-upstream.json`` and written to
    ``<setup-dir>/kas-bbsetup.yml``.
    """
    setup_dir = _bbsetup_workspace(workspace) if manifest is None else None
    if setup_dir is not None:
        if dry_run:
            print(f"output: {setup_dir / 'kas-bbsetup.yml'}")
            print(f"source: {setup_dir / 'config' / 'config-upstream.json'}")
            raise typer.Exit(0)
        out_path = write_bbsetup_yaml(
            setup_dir,
            target=image or "core-image-minimal",
            machine_override=machine,
            distro_override=distro,
        )
        console.print(f"[green]wrote[/] {out_path}")
        return

    family, bsp = _dispatch_bsp(manifest)
    ws = workspace or _workspace_from_cwd()
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(machine=machine, distro=distro, image=image, manifest=manifest, repo_branch=branch),
        user_config=_state._USER_CONFIG,
    )
    out_path = output.resolve() if output is not None else cfg.default_kas_yaml
    if dry_run:
        print(f"output: {out_path}")
        print(f"source: {cfg.manifest_path}")
        raise typer.Exit(0)
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
