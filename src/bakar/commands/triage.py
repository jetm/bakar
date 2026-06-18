"""bakar triage subcommand - post-mortem the last build run."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer

from bakar.bsp_detect import detect_kas_workspace, is_meta_avocado_yaml
from bakar.commands._app import app, console
from bakar.commands._helpers import _bbsetup_workspace, _find_run, _workspace_from_cwd
from bakar.triage import _last_event_matching, _tail, _translate_container_path, analyse

_BITBAKE_EVENTS_FILENAME = "bitbake-events.json"

# Tail length for the structured logfile excerpt. Reuses the same count
# analyse()/_tail() applies to the kas.log / recipe-log excerpts today
# (bakar.triage._tail default and the n=60 call sites) so the structured
# path renders the same amount of context as the legacy fallback.
_LOGFILE_EXCERPT_LINES = 60


def _run_has_failure(run_dir: Path) -> bool:
    """Report whether a run directory recorded a build failure.

    Prefers the structured ``bitbake-events.json`` (a non-empty
    ``failures[]``); falls back to a ``step_fail`` event in
    ``events.jsonl`` for run dirs predating the artifact. A run dir that
    records neither is treated as not-failed so default selection skips
    successful runs.
    """
    if _read_structured_failures(run_dir):
        return True
    return _last_event_matching(run_dir / "events.jsonl", "step_fail") is not None


def _read_structured_failures(run_dir: Path) -> list[dict] | None:
    """Return the normalized ``failures[]`` list, or ``None`` when absent.

    ``None`` signals "no structured artifact for this run" so the caller
    falls back to the ``kas.log`` analysis. An artifact present but with an
    empty ``failures[]`` returns ``[]`` (a parsed-but-clean run), which is
    distinct from ``None``.
    """
    events_path = run_dir / _BITBAKE_EVENTS_FILENAME
    if not events_path.is_file():
        return None
    try:
        data = json.loads(events_path.read_text())
    except json.JSONDecodeError, UnicodeDecodeError, OSError:
        # UnicodeDecodeError (a corrupt-UTF-8 artifact) is not an OSError, so it
        # must be named explicitly to fall back to kas.log rather than crash.
        return None
    if not isinstance(data, dict):
        return None
    failures = data.get("failures")
    if not isinstance(failures, list):
        return []
    return [f for f in failures if isinstance(f, dict)]


def _print_structured_failures(failures: list[dict], workspace: Path) -> None:
    """Render the structured failure records, resolving each container
    ``logfile`` to a host path via the shared ``_translate_container_path``
    helper before reading its tail.
    """
    for failure in failures:
        recipe = failure.get("recipe") or "?"
        task = failure.get("task") or "?"
        console.print(f"[red]✗[/] recipe [bold]{recipe}[/] task [bold]{task}[/] failed")

        logfile = failure.get("logfile")
        if not logfile:
            continue
        host_path = Path(_translate_container_path(str(logfile), workspace))
        # Containment guard: logfile comes from bitbake-events.json, which a
        # crafted artifact could point at a host file outside the build tree
        # (e.g. a secret). Only read paths that resolve under the workspace.
        try:
            host_path.resolve().relative_to(workspace.resolve())
        except ValueError, OSError:
            console.print(f"[dim]logfile outside workspace, not read: {host_path}[/]")
            continue
        tail = _tail(host_path, _LOGFILE_EXCERPT_LINES)
        if not tail:
            console.print(f"[dim]logfile (unreadable on host): {host_path}[/]")
            continue
        console.print(f"[bold]task log:[/] {host_path}")
        console.print(f"[dim]{host_path.name} (tail):[/]")
        for line in tail:
            sys.stdout.write(f"  {line.rstrip()}\n")
        sys.stdout.flush()


def _resolve_triage_dirs(
    kas_yaml: Path | None,
    workspace: Path | None,
) -> tuple[list[tuple[Path, Literal["nxp", "ti", "generic"]]], Path, str, Path]:
    """Resolve the run directories, report root, not-found label, and container
    translation workspace for triage.

    Handles three workspace shapes: BYO kas YAML, bbsetup workspace, and
    standard nxp/ti/avocado workspace. The standard shape also discovers
    preset fan-out run dirs (``<ws>/build/<preset-subdir>/build/runs``) so
    triage can select among the per-release run dirs one ``bakar build
    --preset`` produces.

    The fourth element (``translation_workspace``) is the root against which
    container ``/work/`` paths are translated to host paths. It differs from
    ``report_root`` only for meta-avocado kas YAMLs, where ``KAS_WORK_DIR``
    is the yaml's grandparent (workspace root) rather than ``bsp_root``.
    """
    if kas_yaml is not None:
        resolved = kas_yaml.resolve()
        if is_meta_avocado_yaml(resolved):
            build_root = detect_kas_workspace(resolved) / f"build-{resolved.stem}"
            translation_workspace = build_root.parent
        else:
            build_root = resolved.parent
            translation_workspace = build_root
        runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = [
            (build_root / "build" / "runs", "generic"),
        ]
        report_root = build_root
        not_found_label = f"{runs_dirs[0][0]}"
    elif (setup_dir := _bbsetup_workspace(workspace)) is not None:
        runs_dirs = [(setup_dir / "build" / "runs", "generic")]
        # Preset fan-out: bbsetup releases write to
        # setup_dir/build/<subdir>/build/runs (one subdir deeper than the root
        # run dir above), so discover those too or a multi-release preset run
        # is invisible to default selection and --preset/--release.
        runs_dirs.extend((d, "generic") for d in sorted(setup_dir.glob("build/*/build/runs")))
        report_root = setup_dir
        translation_workspace = setup_dir
        not_found_label = f"{runs_dirs[0][0]}"
    else:
        ws = workspace or _workspace_from_cwd()
        # meta-avocado builds land in ws/build-<stem>/build/runs/
        avocado_dirs = sorted(ws.glob("build-*/build/runs"))
        if avocado_dirs:
            runs_dirs = [(d, "generic") for d in avocado_dirs]
            not_found_label = " or ".join(str(d) for d in avocado_dirs[:2])
        else:
            runs_dirs = [
                (ws / "nxp" / "build" / "runs", "nxp"),
                (ws / "ti" / "build" / "runs", "ti"),
            ]
            # Preset fan-out: bbsetup/generic releases write to
            # ws/build/<subdir>/build/runs, while nxp/ti releases nest one
            # level deeper at ws/build/<subdir>/<family>/build/runs because
            # their bsp_root is workspace/<family>. compose_preset_output_path()
            # encodes <distro>-<machine>-<version> in <subdir>, which
            # --preset/--release match against.
            runs_dirs.extend((d, "generic") for d in sorted(ws.glob("build/*/build/runs")))
            runs_dirs.extend((d, "nxp") for d in sorted(ws.glob("build/*/nxp/build/runs")))
            runs_dirs.extend((d, "ti") for d in sorted(ws.glob("build/*/ti/build/runs")))
            not_found_label = "nxp/build/runs/ or ti/build/runs/"
        report_root = ws
        translation_workspace = ws
    return runs_dirs, report_root, not_found_label, translation_workspace


def _select_run(
    runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]],
    *,
    run_id: str | None,
    preset: str | None,
    release: str | None,
) -> tuple[Path, Literal["nxp", "ti", "generic"]] | None:
    """Choose the run dir to triage among the resolved candidates.

    An explicit ``run_id`` (from the positional argument or ``--run``)
    delegates to ``_find_run`` and overrides every other selector. The
    ``--preset``/``--release`` selectors filter candidates by a
    case-insensitive substring match against the run dir's resolved path
    (the preset subdir name encodes distro/machine/release). With no
    selector the default is the most-recent run dir that recorded a
    failure; when no candidate recorded a failure (or when only one
    candidate exists at all), it falls back to the single most-recent run
    so today's single-build behavior is preserved.
    """
    if run_id is not None:
        return _find_run(runs_dirs, run_id)

    candidates: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = []
    for runs_dir, label in runs_dirs:
        if not runs_dir.is_dir():
            continue
        candidates.extend((entry, label) for entry in runs_dir.iterdir() if entry.is_dir())

    if preset:
        needle = preset.lower()
        candidates = [c for c in candidates if needle in str(c[0].resolve()).lower()]
    if release:
        needle = release.lower()
        candidates = [c for c in candidates if needle in str(c[0].resolve()).lower()]

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0].name, reverse=True)

    # Preserve single-build behavior: one candidate means no fan-out choice.
    if len(candidates) == 1:
        return candidates[0]

    for run_dir, label in candidates:
        if _run_has_failure(run_dir):
            return (run_dir, label)
    # No candidate recorded a failure; fall back to the most recent.
    return candidates[0]


def _emit_triage_json(
    run_id: str,
    failing_step: str | None,
    fail_reason: str | None,
    recipe_log: str | None,
    suggestions: list[str],
) -> None:
    doc = {
        "version": 1,
        "run_id": run_id,
        "failing_step": failing_step,
        "fail_reason": fail_reason,
        "recipe_log": recipe_log,
        "suggestions": suggestions,
    }
    typer.echo(json.dumps(doc, indent=2))


@app.command()
def triage(
    run_id: Annotated[str | None, typer.Argument(help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted.")] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Run ID to triage (alias for the positional argument; takes precedence)."),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="Restrict run-dir selection to a preset (matches the preset build subdir name)."),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option(
            "--release",
            help="Restrict run-dir selection to a release (matches the release/version in the build subdir name).",
        ),
    ] = None,
    kas_yaml: Annotated[
        Path | None,
        typer.Option(
            "--kas-yaml",
            "-k",
            help="kas YAML for a BYO build; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output triage result as JSON instead of formatted text."),
    ] = False,
) -> None:
    """Surface the last failed step of the named run (or the most recent).

    Without ``--kas-yaml`` searches both ``nxp/build/runs/`` and
    ``ti/build/runs/`` under the workspace, plus any preset fan-out run dirs
    under ``build/<preset-subdir>/build/runs/``. Pass ``--kas-yaml my.yml``
    for a BYO build whose runs live next to the YAML
    (``<yaml-parent>/build/runs/``); the BSP family is inferred from
    the run directory's location and reported as ``generic`` for
    generic BYO YAMLs.

    Under preset fan-out (multiple candidate run dirs) the default is the
    most-recent run dir that recorded a failure. Use ``--run`` (or the
    positional run ID), ``--preset``, or ``--release`` to override that
    default selection. When the selected run dir holds a
    ``bitbake-events.json`` artifact, the failing recipe/task and a tail of
    the recorded task logfile are read from it; otherwise triage falls back
    to its ``kas.log`` analysis.
    """
    effective_run_id = run if run is not None else run_id
    runs_dirs, report_root, not_found_label, translation_workspace = _resolve_triage_dirs(kas_yaml, workspace)
    found = _select_run(runs_dirs, run_id=effective_run_id, preset=preset, release=release)
    if found is None:
        if effective_run_id:
            console.print(f"[red]Run {effective_run_id} not found under {not_found_label}[/]")
        else:
            console.print(f"[yellow]No runs found under {not_found_label}.[/]")
        raise typer.Exit(code=1)

    run_dir, _label = found
    if not output_json:
        console.print(f"[bold]::[/] triage {run_dir.name}")

    # Structured-failure-first: when the normalized artifact is present, name
    # the failing recipe/task and print the recorded logfile excerpt instead
    # of scraping kas.log. Absent artifact (or no recorded failures) falls
    # through to the kas.log analysis below.
    structured = _read_structured_failures(run_dir)
    if output_json and structured is not None:
        if structured:
            f0 = structured[0]
            logfile = f0.get("logfile")
            _emit_triage_json(
                run_dir.name, f"{f0.get('recipe')}:{f0.get('task')}", None, str(logfile) if logfile else None, []
            )
        else:
            _emit_triage_json(run_dir.name, None, None, None, [])
        return
    if structured:
        _print_structured_failures(structured, translation_workspace)
        return

    report = analyse(run_dir, report_root)
    if output_json:
        _emit_triage_json(
            run_dir.name,
            report.failing_step,
            report.fail_reason,
            str(report.recipe_log) if report.recipe_log else None,
            list(report.suggestions),
        )
        return

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
