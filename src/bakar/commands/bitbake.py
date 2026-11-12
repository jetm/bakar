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

import contextlib
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

import bakar.commands._app as _state
from bakar.commands import _detach_cmd, _hints
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _combine_overlays_with_tuning,
    _host_extra_overlays,
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
    invoking_cwd,
    resolve_global_config,
    split_kas_yaml_arg,
)
from bakar.config import BSPSpec, ResolveRequest
from bakar.observability import RunLogger
from bakar.steps.kas_build import (
    KasBuildContext,
    ShellCapture,
    materialize_cache_classify_layer,
    materialize_host_layer,
    materialize_sccache_layer,
    persist_run_artifacts,
    run_shell,
    run_shell_capture,
    run_shell_live,
)
from bakar.steps.kas_graph_capture import GRAPH_CAPTURE_TIMEOUT_S
from bakar.triage import translate_container_path

# unsupported: the command has no --detach; offered: it has one the caller did not
# pass (agent callers get a nudge); requested: --detach was passed.
type DetachMode = Literal["unsupported", "offered", "requested"]


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


@contextlib.contextmanager
def _capture_context(cfg, kas_yaml: str | Path) -> Iterator[tuple[KasBuildContext, Path]]:
    """Build the kas context ``bakar getvar`` builds, yielding it with its run dir."""
    main_yaml, user_extras = split_kas_yaml_arg(kas_yaml)
    _family, bsp, main_yaml, _manifest = _normalize_dispatch(main_yaml, None)
    overlay_source = _overlay_for(bsp)
    extra_overlays = _combine_overlays_with_tuning(user_extras, cfg)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        yield KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source, extra_overlays=extra_overlays), log.run_dir


def _file_lookup_command(recipes: list[str]) -> str:
    """One shell loop printing ``<recipe>\t<FILE>`` per recipe, empty FILE on failure.

    One loop yields one exit code, so every recipe prints a line whatever
    happens to it; the `if` keeps a failing lookup from aborting the rest.
    bitbake-getvar can print a line (a server NOTE) before the value, so only
    the last line of its output is kept.
    """
    names = " ".join(shlex.quote(r) for r in recipes)
    return (
        f"for r in {names}; do "
        'if v=$(bitbake-getvar --value --ignore-undefined -r "$r" FILE); '
        "then v=${v##*$'\\n'}; printf '%s\\t%s\\n' \"$r\" \"$v\"; "
        "else printf '%s\t\n' \"$r\"; fi; done"
    )


class RecipeFiles(dict[str, Path | None]):
    """Recipe to host ``.bb`` path (None when unresolved), plus the lookup's own run.

    ``run_id`` names the run record the lookup opened (its ``recipe-files.err``
    holds bitbake's complaints); None when the lookup never started.
    """

    run_id: str | None = None


def resolve_recipe_files(
    cfg,
    kas_yaml: str | Path,
    recipes: list[str],
    *,
    timeout: float = GRAPH_CAPTURE_TIMEOUT_S,
    runner: Callable[[KasBuildContext, str, ShellCapture], int] = run_shell_capture,
) -> RecipeFiles:
    """Map each recipe to its host ``.bb`` path, or None when it cannot be resolved.

    Runs ``bitbake-getvar -r <recipe> FILE`` for every recipe in one kas shell.
    Never raises for a per-recipe failure, a timeout or a failure to start:
    those all map to None so the caller can fall back per recipe. A timeout or
    a non-zero exit keeps the answers already written to the log.
    """
    result = RecipeFiles(dict.fromkeys(recipes))
    if not recipes:
        return result
    # A refresh must not die because a lookup could not run: any failure here
    # (kas missing, lock held, timeout, bad overlay) degrades to "unresolved".
    text = ""
    try:
        with _capture_context(cfg, kas_yaml) as (kas_ctx, run_dir):
            result.run_id = run_dir.name
            capture_path = run_dir / "recipe-files.log"
            try:
                runner(
                    kas_ctx,
                    _file_lookup_command(recipes),
                    ShellCapture(
                        stdout_path=capture_path,
                        stderr_path=run_dir / "recipe-files.err",
                        step="recipe_files",
                        env_overrides={"SHELL": "/bin/bash"},
                        timeout=timeout,
                        isolate_process_group=True,
                    ),
                )
            finally:
                # A timeout or a non-zero exit still leaves the recipes answered so far in the log.
                text = capture_path.read_text(errors="replace") if capture_path.exists() else ""
    # RuntimeError covers a held workspace lock and the missing-buildtools/bitbake errors.
    except OSError, RuntimeError, subprocess.TimeoutExpired, typer.Exit, typer.BadParameter:
        pass

    # Only whole lines count: a run cut off mid-write can leave a truncated path.
    for line in text.split("\n")[:-1]:
        name, sep, raw = line.partition("\t")
        raw = raw.strip()
        if not sep or name not in result or not raw:
            continue
        # kas-container binds kas_work_dir at /work; host mode has no such mount.
        translated = raw if cfg.host_mode else translate_container_path(raw, cfg.kas_work_dir)
        # A relative or junk answer must not be resolved against bakar's own cwd.
        if not str(translated).startswith("/"):
            continue
        result[name] = Path(os.path.realpath(translated))
    return result


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


def _prepare_run(
    *,
    manifest: str | None,
    machine: str | None,
    workspace: Path | None,
    kas_yaml: str | None,
):
    """Resolve config and materialize overlay layers for a kas-container run.

    Shared by ``_run_task`` and ``build_targets`` so both dispatch identically.
    Returns ``(cfg, overlay_source, extra_overlays)``.
    """
    main_yaml, user_extras = split_kas_yaml_arg(kas_yaml)
    family, bsp, main_yaml, manifest = _normalize_dispatch(main_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family)
    cfg = resolve_global_config(
        ResolveRequest(
            workspace=ws,
            bsp_family=family,
            spec=BSPSpec(manifest=manifest, machine=machine),
            kas_yaml=main_yaml,
            user_config=_state._USER_CONFIG,
        )
    )
    overlay_source = _overlay_for(bsp)
    # The cache-classify overlay is the one unconditional entry in
    # _tuning_extra_overlays - every build references it, so materialize it
    # unconditionally too, unlike the sccache/host layers below.
    materialize_cache_classify_layer(cfg)
    if cfg.use_sccache_dist:
        materialize_sccache_layer(cfg)
    extra_overlays = _combine_overlays_with_tuning(user_extras, cfg)
    # Host mode needs the meta-bakar-host layer (rpm transaction-plugin guard)
    # even when sccache is off, where the branch above skips tuning overlays.
    # Materialize the layer and append its overlay, deduped against whatever the
    # sccache branch already combined in.
    if cfg.host_mode:
        materialize_host_layer(cfg)
        extra_overlays = [*extra_overlays, *(o for o in _host_extra_overlays(cfg) if o not in extra_overlays)]
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    return cfg, overlay_source, extra_overlays


@dataclass(frozen=True)
class BuildResult:
    """Outcome of ``build_targets``: bitbake's exit code and the run id."""

    exit_code: int
    run_id: str


def _run_task(
    target: str,
    task: str | None,
    *,
    keep_going: bool,
    manifest: str | None,
    machine: str | None,
    workspace: Path | None,
    kas_yaml: str | None,
    step: str,
    command_override: str | None = None,
    detach_mode: DetachMode = "unsupported",
) -> None:
    """Dispatch and run a bitbake task for ``target`` inside kas-container.

    Routing:

    - ``task == "devshell"``: interactive ``run_shell`` (inherited terminal),
      no capture.
    - ``task == "listtasks"``: capture and pretty-print the parsed task names.
    - otherwise: stream the live knotty UI via ``run_shell_live`` and exit with
      bitbake's exit code.

    ``command_override`` replaces the single-invocation command built from
    ``target``/``task`` with a prebuilt shell string (e.g. ``rebuild``'s
    ``cleansstate && build`` chain). Only valid on the live path - callers that
    pass it leave ``task`` None so the devshell/listtasks branches are skipped.

    ``detach_mode`` is ``"unsupported"`` for commands that do not offer
    ``--detach``. ``"requested"`` hands the invocation to a transient unit before
    anything is resolved, and ``"offered"`` lets agent callers see a nudge toward
    it on the live path.
    """
    if detach_mode == "requested":
        if task == "devshell":
            console.print("[red]--detach cannot be combined with --task devshell (interactive).[/]")
            raise typer.Exit(code=2)
        _detach_cmd.detach_and_exit(sys.argv[1:], invoking_cwd())
    if detach_mode == "offered" and _hints.agent_mode() and task not in ("listtasks", "devshell"):
        _hints.emit(_hints.foreground_nudge())

    cfg, overlay_source, extra_overlays = _prepare_run(
        manifest=manifest, machine=machine, workspace=workspace, kas_yaml=kas_yaml
    )

    command = command_override if command_override is not None else _build_command(target, task, keep_going=keep_going)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source, extra_overlays=extra_overlays)

        # devshell is interactive and cannot be captured; route through the
        # inherited-terminal path.
        if task == "devshell":
            rc = run_shell(kas_ctx, [], command=command)
            raise typer.Exit(code=rc)

        if task == "listtasks":
            stdout_path = log.run_dir / f"{step}.log"
            rc = run_shell_capture(kas_ctx, command, ShellCapture(stdout_path=stdout_path, step=step))
            persist_run_artifacts(cfg, log)
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
        persist_run_artifacts(cfg, log)
        if rc != 0:
            console.print(f"[red]{command} failed (exit {rc}, run {log.run_id}).[/]")
        if _hints.agent_mode():
            _hints.emit(_hints.after_finish(log.run_id, rc))
        raise typer.Exit(code=rc)


def build_targets(
    targets: list[str],
    *,
    keep_going: bool = False,
    manifest: str | None = None,
    machine: str | None = None,
    workspace: Path | None = None,
    kas_yaml: str | None = None,
) -> BuildResult:
    """Build several targets in one bitbake invocation and return the outcome.

    Unlike ``_run_task`` this never raises ``typer.Exit``, so a caller can act
    on a failed build (it gets the exit code and run id back). The command is
    ``bitbake [-k] <t1> <t2> ...`` streamed live through ``run_shell_live``.
    """
    if not targets:
        raise ValueError("build_targets needs at least one target")
    cfg, overlay_source, extra_overlays = _prepare_run(
        manifest=manifest, machine=machine, workspace=workspace, kas_yaml=kas_yaml
    )
    parts = ["bitbake"]
    if keep_going:
        parts.append("-k")
    parts += [shlex.quote(t) for t in targets]
    command = " ".join(parts)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source, extra_overlays=extra_overlays)
        rc = run_shell_live(kas_ctx, command)
        persist_run_artifacts(cfg, log)
        if rc != 0:
            console.print(f"[red]{command} failed (exit {rc}, run {log.run_id}).[/]")
        return BuildResult(exit_code=rc, run_id=log.run_id)


@app.command()
def bitbake(
    target: Annotated[
        str,
        typer.Argument(help="Recipe or image target to build (e.g. busybox, core-image-minimal)."),
    ],
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="Optional kas YAML (BYO/bbsetup); supports colon-overlay syntax: machine.yml:overlay.yml.",
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
    workspace: WorkspaceOption = None,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Run in a transient systemd unit and print a JSON handle."),
    ] = False,
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
        detach_mode="requested" if detach else "offered",
    )


@app.command("clean-recipe")
def clean_recipe(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe to clean via bitbake -c cleansstate."),
    ],
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="Optional kas YAML (BYO/bbsetup); supports colon-overlay syntax: machine.yml:overlay.yml.",
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
    workspace: WorkspaceOption = None,
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


@app.command()
def rebuild(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe to rebuild from scratch (cleansstate, then build)."),
    ],
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="Optional kas YAML (BYO/bbsetup); supports colon-overlay syntax: machine.yml:overlay.yml.",
        ),
    ] = None,
    keep_going: Annotated[
        bool,
        typer.Option("--keep-going", "-k", help="Pass -k to the build phase (keep building after failures)"),
    ] = False,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", "-m", help="Override the target machine"),
    ] = None,
    workspace: WorkspaceOption = None,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Run in a transient systemd unit and print a JSON handle."),
    ] = False,
) -> None:
    """Rebuild a recipe from scratch: ``bitbake -c cleansstate <recipe> && bitbake <recipe>``.

    Wipes the recipe's sstate and work directory, then rebuilds it, in one
    kas-container invocation logged to the run dir. Use when a recipe's cached
    output is stale or corrupt and a plain ``bakar bitbake`` would pull the bad
    sstate. The ``&&`` chain short-circuits: a failed cleansstate skips the
    build. Exits with bitbake's own exit code.
    """
    clean = _build_command(recipe, "cleansstate", keep_going=False)
    build = _build_command(recipe, None, keep_going=keep_going)
    _run_task(
        recipe,
        None,
        keep_going=keep_going,
        manifest=manifest,
        machine=machine,
        workspace=workspace,
        kas_yaml=kas_yaml,
        step="rebuild",
        command_override=f"{clean} && {build}",
        detach_mode="requested" if detach else "offered",
    )
