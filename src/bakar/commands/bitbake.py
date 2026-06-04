"""bakar bitbake and clean-recipe subcommands - recipe-level passthrough.

``bakar bitbake <target>`` runs ``bitbake <target>`` (or ``bitbake -c <task>
<target>``) inside kas-container for any workspace family, with the run
logged to the per-run dir. ``bakar clean-recipe <recipe>`` is a thin alias
for ``bitbake -c cleansstate <recipe>``.

Two task names are special-cased:

- ``--task devshell`` is interactive and routes through ``run_shell`` with an
  inherited terminal; its output is never captured to a log.
- ``--task listtasks`` captures ``bitbake -c listtasks <target>`` and
  pretty-prints the parsed task names.

Every other invocation streams bitbake output live through the knotty UI and
exits with bitbake's own exit code, surfacing a non-zero result rather than
reporting success.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.steps.kas_build import (
    KasBuildContext,
    copy_oe_eventlog_to_run_dir,
    run_shell,
    run_shell_capture,
    run_shell_live,
)


def _build_command(target: str, task: str | None, *, keep_going: bool) -> str:
    """Build the bitbake command line for a target and optional task.

    ``bitbake <target>`` by default, ``bitbake -c <task> <target>`` when
    ``task`` is set, with ``-k`` appended when ``keep_going`` is True. Both
    ``target`` and ``task`` are shell-quoted.
    """
    parts = ["bitbake"]
    if task:
        parts += ["-c", shlex.quote(task)]
    if keep_going:
        parts.append("-k")
    parts.append(shlex.quote(target))
    return " ".join(parts)


def _parse_listtasks(text: str) -> list[str]:
    """Parse ``bitbake -c listtasks <target>`` output into task names.

    listtasks emits one task per line as ``do_<name>``; this extracts the
    leading ``do_*`` token from each line, ignoring NOTE/log noise.
    """
    tasks: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        if first.startswith("do_"):
            tasks.append(first)
    return tasks


def _run_task(
    target: str,
    task: str | None,
    *,
    keep_going: bool,
    manifest: str | None,
    machine: str | None,
    workspace: Path | None,
    kas_yaml: Path | None,
    step: str,
) -> None:
    """Dispatch and run a bitbake task for ``target`` inside kas-container.

    Routing:

    - ``task == "devshell"``: interactive ``run_shell`` (inherited terminal),
      no capture.
    - ``task == "listtasks"``: capture and pretty-print the parsed task names.
    - otherwise: stream the live knotty UI via ``run_shell_live`` and exit with
      bitbake's exit code.
    """
    family, bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest, machine=machine),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    command = _build_command(target, task, keep_going=keep_going)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)

        # devshell is interactive and cannot be captured; route through the
        # inherited-terminal path.
        if task == "devshell":
            rc = run_shell(kas_ctx, [], command=command)
            raise typer.Exit(code=rc)

        if task == "listtasks":
            stdout_path = log.run_dir / f"{step}.log"
            rc = run_shell_capture(kas_ctx, command, stdout_path, step=step)
            copy_oe_eventlog_to_run_dir(cfg, log)
            log.persist_bitbake_events()
            out_text = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
            if rc != 0:
                console.print(f"[red]bitbake -c listtasks {target} failed (exit {rc}).[/]\n{out_text}")
                raise typer.Exit(code=rc)
            tasks = _parse_listtasks(out_text)
            console.print(f"[bold]Tasks for {target}:[/]", highlight=False)
            if tasks:
                for t in tasks:
                    console.print(f"  {t}", highlight=False)
            else:
                console.print("  (none)", highlight=False)
            raise typer.Exit(code=0)

        rc = run_shell_live(kas_ctx, command)
        copy_oe_eventlog_to_run_dir(cfg, log)
        log.persist_bitbake_events()
        if rc != 0:
            console.print(f"[red]{command} failed (exit {rc}).[/]")
        raise typer.Exit(code=rc)


@app.command()
def bitbake(
    target: Annotated[
        str,
        typer.Argument(help="Recipe or image target to build (e.g. busybox, core-image-minimal)."),
    ],
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO/bbsetup); resolves the workspace next to it.",
        ),
    ] = None,
    task: Annotated[
        str | None,
        typer.Option("--task", "-c", help="bitbake task to run (e.g. compile, listtasks, devshell)"),
    ] = None,
    keep_going: Annotated[
        bool,
        typer.Option("--keep-going", "-k", help="Pass -k to bitbake (keep building after failures)"),
    ] = False,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", "-m", help="Override the target machine"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Run ``bitbake <target>`` inside kas-container, logged to the run dir.

    \b
    Default: bitbake <target>
    --task/-c <task>: bitbake -c <task> <target>
    --keep-going/-k:  append -k

    \b
    --task listtasks: capture and pretty-print the recipe's task names
    --task devshell:  drop into an interactive devshell (TTY attached)

    Exits with bitbake's own exit code, surfacing a non-zero result rather
    than reporting success.
    """
    _run_task(
        target,
        task,
        keep_going=keep_going,
        manifest=manifest,
        machine=machine,
        workspace=workspace,
        kas_yaml=kas_yaml,
        step="bitbake",
    )


@app.command("clean-recipe")
def clean_recipe(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe to clean via bitbake -c cleansstate."),
    ],
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO/bbsetup); resolves the workspace next to it.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", "-m", help="Override the target machine"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Run ``bitbake -c cleansstate <recipe>`` inside kas-container.

    Convenience alias for the most common cleanup task; shares the same
    task-execution path as ``bakar bitbake``, logged to the run dir, and
    exits with bitbake's own exit code.
    """
    _run_task(
        recipe,
        "cleansstate",
        keep_going=False,
        manifest=manifest,
        machine=machine,
        workspace=workspace,
        kas_yaml=kas_yaml,
        step="clean-recipe",
    )
