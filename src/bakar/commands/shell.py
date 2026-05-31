"""bakar shell and run subcommands - interactive kas-container and QEMU execution."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _dispatch_from_yaml,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import resolve
from bakar.observability import RunLogger
from bakar.steps import kas_build as step_kas
from bakar.steps.kas_build import KasBuildContext
from bakar.steps import run_qemu as step_run


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
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)
        rc = step_kas.run_shell(
            kas_ctx,
            list(extra or []),
            command=command,
        )
    raise typer.Exit(code=rc)


@app.command()
def run(
    kas_yaml: Annotated[
        str,
        typer.Argument(
            help="kas YAML identifying the target machine. Accepts colon-separated overlays "
            "(e.g. kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml); overlays are "
            "ignored for machine resolution but allow the same invocation used with build.",
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
    ``bakar build kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml``
    first to produce the bootable disk image.

    Only supported for meta-avocado builds (avocado-qemux86-64, avocado-qemuarm64).
    """
    main_yaml = Path(kas_yaml.split(":")[0])
    family, _ = _dispatch_from_yaml(main_yaml)
    ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        kas_yaml=main_yaml,
        user_config=_state._USER_CONFIG,
    )

    if not cfg.is_meta_avocado:
        console.print("[red]bakar run currently only supports meta-avocado builds[/]")
        raise typer.Exit(code=2)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        try:
            rc = step_run.run_qemu(cfg, log, swtpm=swtpm, kas_yaml=main_yaml)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=2) from None
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from None
    raise typer.Exit(code=rc)
