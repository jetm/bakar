"""Helpers shared by the build command and its flavor dispatchers.

Lives apart from build.py so _build_flavors can import them without importing
build, which itself imports _build_flavors.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import typer
from rich.console import Console

import bakar.commands._app as _state
import bakar.commands._hints as _hints
from bakar.commands._app import console
from bakar.commands._helpers import _print_sstate_summary, global_output_mode_override
from bakar.commands._post_build import (
    _CveRequest,
    _FeedRequest,
    _filter_image_sbom,
    _generate_cve_report,
    _SbomRequest,
    _sync_feed,
)
from bakar.fmt import fmt_duration
from bakar.observability import RunLogger
from bakar.output_mode import OutputMode, resolve_output_mode
from bakar.steps.kas_build import KasBuildContext

if TYPE_CHECKING:
    from pathlib import Path


def _output_mode() -> OutputMode:
    """Resolve the human-output mode from the global override and this run's stream."""
    return resolve_output_mode(global_output_mode_override(), isatty=sys.stderr.isatty(), ci_env=os.environ.get("CI"))


def _plain_render_console() -> Console | None:
    """A no-color render console for RunLogger under plain mode, else None (module default).

    Keeps the out-of-Live summary/hint lines, layer tables, and alerts ANSI-free even on a
    forced ``--plain`` TTY (design D9).
    """
    if _output_mode() is OutputMode.PLAIN:
        # stderr for the same reason ``commands/_app.py`` uses it, and that
        # module carries the full rationale: stdout is reserved for
        # machine-readable payloads, so everything a human reads goes here.
        # A RunLogger render console is diagnostics by definition.
        return Console(no_color=True, force_terminal=False, stderr=True)
    return None


def _make_kas_ctx(cfg, log, overlay_source: Path, ctx) -> KasBuildContext:
    """Build a KasBuildContext for run_build, threading the shared mode config.

    ``ctx`` is either ``_BbsetupCtx`` or ``_BuildCtx``; both carry keep_going,
    dry_run, and target, the only fields callers need beyond cfg/log/overlay.
    """
    return KasBuildContext(
        cfg,
        log,
        cfg.kas_yaml,
        overlay_source,
        keep_going=ctx.keep_going,
        dry_run=ctx.dry_run,
        target=ctx.target,
        output_mode=_output_mode(),
    )


def _open_run_logger(cfg) -> RunLogger:
    """Open a RunLogger honoring the plain-mode render console override."""
    return RunLogger(runs_dir=cfg.runs_dir, render_console=_plain_render_console())


def _finish_build(
    cfg,
    log,
    rc: int,
    machine: str,
    feed: _FeedRequest | None = None,
    cve: _CveRequest | None = None,
    sbom: _SbomRequest | None = None,
) -> None:
    """Shared build tail: rc check + triage hint, sstate summary, success line, artifacts path.

    ``machine`` names the deploy/images subdir - ``cfg.machine`` for byo/manifest
    builds, the bbsetup-translated machine for the bbsetup path.

    ``feed`` drives a sync then an index once the build has succeeded. The work
    sits here rather than in the callers because this function already returns
    early on a non-zero rc: a failed build cannot reach it, so "success only"
    holds for every call site including ones added later, rather than depending
    on each remembering to guard. A partial deploy staged into the feed would
    render a repository the index then advertises as installable.

    ``cve`` is the context the build just ran with, present only when ``--cve``
    was passed. The report is produced by a SECOND bitbake invocation derived
    from it, for the same reason it sits here rather than in the image graph:
    ``avocado-cve-report`` joins cve-check results across the whole tree, so a
    run scheduled alongside the ``do_cve_check`` tasks it reads would summarise a
    scan still in progress.

    A dry run is NOT such a success and the callers filter it out before this
    point, because ``run_build`` returns 0 after printing its preview - so rc
    alone cannot tell "built" from "never ran".
    """
    if rc != 0:
        exe = "kas" if cfg.host_mode else "kas-container"
        console.print(
            f"[red]{exe} build failed (exit {rc}, run {log.run_id}).[/] Run `bakar triage {log.run_id}` for details."
        )
        if _hints.agent_mode():
            _hints.emit(_hints.after_finish(log.run_id, rc))
        raise typer.Exit(code=rc)
    # resolved_tmpdir is the single source of truth for the build TMPDIR: it
    # honors a local_tmpdir_base override (so the banner points at the real
    # images) and already encodes the family tmp-dir layout (tmp-glibc for qcom,
    # tmp elsewhere).
    deploy = cfg.resolved_tmpdir / "deploy" / "images" / machine
    if _state._USER_CONFIG is not None and _state._USER_CONFIG.show_sstate_summary:
        _print_sstate_summary(log.run_dir / "kas.log")
    console.print(
        f"[bold green]build succeeded[/] in {fmt_duration(time.monotonic() - log.start_monotonic)} (run {log.run_id})"
    )
    if _hints.agent_mode():
        _hints.emit(_hints.after_finish(log.run_id, rc))
    console.print(f"artifacts: {deploy}")

    # Before the feed sync: this reads the build tree and writes one JSON beside
    # the images, while the sync publishes that tree outward. Keeping every
    # build-tree step ahead of the publish keeps the order readable, and the two
    # do not otherwise interact - the report recipe inherits ``nopackages`` and
    # deletes its install and sysroot tasks, so it adds no RPM the feed could
    # pick up.
    if cve is not None:
        _generate_cve_report(cfg, log, cve, machine)

    # Also before the feed sync, and for a sharper reason than the CVE report's:
    # this produces the document a later publish step puts INTO the feed, so it
    # has to exist and be checked before anything publishes.
    filtered_sboms = _filter_image_sbom(cfg, log, sbom) if sbom is not None else []

    if feed is not None:
        # Only what THIS invocation filtered and checked reaches the feed. Reading
        # the output directory instead would publish whatever a previous run left
        # there, including a document produced before the filter was updated.
        _sync_feed(cfg, log, feed, sboms=filtered_sboms)
