"""PTY and Live-UI orchestration for the kas build step.

Split out of :mod:`bakar.steps.kas_build` (task 10.2). Every symbol here is
re-exported at ``bakar.steps.kas_build``, and the test suite monkeypatches
``kas_build._run_pty_with_ui``, ``kas_build._PLAIN_STATUS_INTERVAL``, and
``kas_build.subprocess.Popen`` expecting those patches to reach this module's
code. ``subprocess`` is a shared singleton module object, so patching an
attribute on it works regardless of which module imported it. A patch on a
NAME bound in ``kas_build``'s own namespace (``_run_pty_with_ui``,
``_PLAIN_STATUS_INTERVAL``) does not propagate here automatically, so
``_PlainFrameController._loop`` reads ``_PLAIN_STATUS_INTERVAL`` through a
deferred ``kas_build`` module reference rather than as a bare global.

``_build_env``/``_container_eventlog_path`` stay in :mod:`bakar.steps.kas_build`
(they are general build-env assembly, not PTY/UI-specific) and are reached
here the same way, via a function-body deferred import - both to avoid a
module-level import cycle with ``kas_build`` (which imports this module to
re-export ``_run_pty_with_ui`` et al.) and because neither is imported
elsewhere as a name into this module's own namespace.
"""

from __future__ import annotations

import os
import pty
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.live import Live

from bakar import build_scope, build_stop, journal
from bakar.cache_render import (
    build_end_summary_plain,
    build_end_summary_rich,
    cache_delta,
    cache_hit_pct,
    ccache_doc,
    cluster_doc,
    daemon_doc,
    render_ccache_cache,
    render_cluster,
    render_sccache_cache,
)

# ``BuildConfig`` and ``RunLogger`` are imported at runtime, not under
# TYPE_CHECKING, because they annotate fields of ``_PtyCtx`` and a guarded
# import leaves ``get_type_hints`` on that dataclass raising ``NameError``
# (see ``test_pty_ctx_carries_every_field_unchanged`` /
# ``test_arity_boundaries.py``, which introspects every packed-context
# dataclass this way).
from bakar.config import BuildConfig  # noqa: TC001 - runtime-required, see the comment above
from bakar.diagnostics import probe_build_daemon, probe_ccache, probe_cluster
from bakar.eventlog import tail_events
from bakar.observability import RunLogger  # noqa: TC001 - runtime-required, see the comment above
from bakar.output_mode import OutputMode
from bakar.steps.build_ui import BuildUIState, _fmt_stall

if TYPE_CHECKING:
    from rich.console import Console

# knotty in TTY mode emits ANSI CSI escapes to manipulate the cursor and
# redraw progress lines in place.  We strip both the standard CSI form
# (ESC [ ... letter) and the less common OSC form (ESC ] ... BEL) before
# writing to kas.log so downstream tools (triage, grep, bakar log) see
# clean plain text.  The regex is deliberately conservative; anything
# exotic gets left as-is.  See ``bakar log`` for the downstream reader.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
LINE_SPLIT_RE = re.compile(rb"\r\n|\n|\r")


def _strip_ansi(s: str) -> str:
    return ANSI_OSC_RE.sub("", ANSI_CSI_RE.sub("", s))


# How often the stall watchdog samples running-task log freshness.
_STALL_POLL_SECS = 30

# How often the error watchdog checks for a task failure. Short on purpose -
# unlike the stall watchdog (which waits out a long silence threshold before
# it even starts caring), this one exists to react as fast as possible once
# ui.had_task_failures flips true, matching _heartbeat's cadence.
_ERROR_POLL_SECS = 1

# Plain-mode status heartbeat tick. The TICK is the throttle: the status thread
# samples the current build state once per interval (level-sampled), so a task
# storm cannot flood the log. plain_status_line() only dedups identical lines.
_PLAIN_STATUS_INTERVAL = 2.0


class _PlainFrameController:
    """Frame controller for plain (CI) output: no Rich ``Live``, a throttled status thread.

    Exposes the exact surface the PTY closures call on a Rich ``Live`` - ``console``,
    ``stop()``, ``start(*, refresh=False)``, and writable ``transient`` /
    ``vertical_overflow`` attributes - as
    no-ops / plain writes, so ``_run_pty_with_ui``'s body runs unchanged. As a context
    manager it starts a daemon thread that prints ``ui.plain_status_line()`` on a fixed
    ``_PLAIN_STATUS_INTERVAL`` tick and joins it on exit (before the caller prints its
    post-build summary), so a stale heartbeat cannot interleave with the final lines.
    """

    def __init__(self, ui: BuildUIState, console: Console, stop_event: threading.Event) -> None:
        self.console = console
        self.transient = False
        # Read and written by the failure-freeze path, which is shared with the
        # Rich branch; plain mode has no live region for it to affect.
        self.vertical_overflow = "ellipsis"
        self._ui = ui
        self._stop_event = stop_event
        self._thread: threading.Thread | None = None

    def stop(self) -> None:
        """No-op: there is no Live region to tear down in plain mode."""

    def start(self, *, refresh: bool = False) -> None:
        """No-op: mirrors ``Live.start(refresh=...)`` so the freeze/restart path is safe."""

    def _loop(self) -> None:  # pragma: no cover - timing-driven daemon thread
        # Deferred: kas_build.probe_cluster-style indirection so
        # `monkeypatch.setattr(kas_build, "_PLAIN_STATUS_INTERVAL", ...)` (a
        # patch on kas_build's own namespace) reaches this loop, which a bare
        # read of the module-level constant here would not.
        from bakar.steps import kas_build

        while not self._stop_event.wait(timeout=kas_build._PLAIN_STATUS_INTERVAL):
            line = self._ui.plain_status_line()
            if line is not None:
                # markup=False: the status line contains literal brackets (e.g.
                # "bakar[build]") that Rich would otherwise parse as style tags.
                self.console.print(line, markup=False)

    def __enter__(self) -> _PlainFrameController:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        # Set the stop event ourselves so the status thread always terminates,
        # even if the body raised before its own stop_event.set() (e.g. a Ctrl-C
        # during thread startup) - otherwise the join would time out with the
        # daemon still emitting heartbeats during teardown.
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * _PLAIN_STATUS_INTERVAL)


@dataclass(slots=True)
class _PtyOutcome:
    """Result of a PTY-driven run: the child exit code plus stall-abort context.

    ``stall_tasks`` is the list of running task labels at the moment the stall
    watchdog aborted the build (``None`` for a normal exit), so the caller can
    record a ``stall-timeout`` step_fail instead of a bare exit code.

    ``cache_backend``/``cache_doc`` carry the active backend name and its
    per-build cache delta (computed at teardown), so the caller can print the
    build-end summary at the post-block site. Both are ``None`` when no cache
    backend was active.
    """

    rc: int | None
    stall_tasks: list[str] | None = None
    cache_backend: str | None = None
    cache_doc: dict | None = None


def _build_fail_reason(rc: int | None, stall_tasks: list[str] | None) -> str:
    """Compose the step_fail reason for a build, naming stuck tasks on a stall abort."""
    if stall_tasks:
        return f"stall-timeout: {', '.join(stall_tasks)}"
    if rc is not None:
        return f"exit_code={rc}"
    return "wrapper-crash"


def _print_cache_summary(log: RunLogger, backend: str | None, doc: dict | None, output_mode: OutputMode) -> None:
    """Print the build-end cache-usage summary at the post-block summary site.

    Called from the runner's finally, after the live frame has closed, so the
    summary cannot interleave with a heartbeat frame. Best-effort: emits nothing
    when no cache backend was active and never crashes a completed build.
    """
    if not doc or backend is None:
        return
    try:
        if output_mode is OutputMode.PLAIN:
            # markup=False: the ``bakar[cache]`` prefix has literal brackets that
            # Rich would otherwise parse as a style tag (as the heartbeat does).
            summary = build_end_summary_plain(doc, backend)
            if summary:
                log.console.print(summary, markup=False)
        else:
            log.console.print(build_end_summary_rich(doc, backend))
    except Exception:  # noqa: BLE001 - best-effort; never crash a completed build
        return


@dataclass(frozen=True, slots=True, kw_only=True)
class _PtyCtx:
    """The eight :func:`_run_pty_with_ui` parameters, packed into one argument.

    Field names match the former parameter names one-for-one, so the body can
    unpack the context back into identically-named locals and leave the nine
    inner closures - the pump, the heartbeat, the event tail, the stall and
    error watchdogs - reading the same free variables they always did. A
    transposed or dropped field is caught by
    ``test_pty_ctx_carries_every_field_unchanged``, which checks both call
    sites field by field, guarded by
    ``test_pty_ctx_has_no_two_fields_sharing_type_and_default`` - no two
    fields here share a type and a default, which is what lets one all-fields
    test stand in for a one-at-a-time sweep.
    """

    cmd: list[str]
    cfg: BuildConfig
    log: RunLogger
    ui: BuildUIState
    stop_event: threading.Event
    show_layers: bool = False
    output_mode: OutputMode = OutputMode.RICH
    scope_unit: str | None = None


def _run_pty_with_ui(ctx: _PtyCtx) -> _PtyOutcome:
    """Run ``ctx.cmd`` under a PTY, pumping its output into ``ctx.ui`` live.

    The pump thread writes every line to kas.log for `bakar log` to tail,
    parses bitbake counters into a rich Progress bar, and surfaces
    ERROR/WARNING/FATAL/QA Issue lines above the bar.  Nothing goes to
    sys.stdout directly - the Progress instance owns the terminal.

    PTY plumbing: openpty() gives us a (master, slave) fd pair. We pass
    slave as the child's stdout/stderr so kas-container's `[ -t 1 ]`
    check sees a TTY and adds `-t -i` to `docker run`, which in turn
    makes bitbake's knotty UI interactive. knotty uses CR (no newline)
    to redraw its status line in place, so we read chunks and split on
    \\r, \\n, or \\r\\n manually instead of line-iterating.

    Returns a :class:`_PtyOutcome` carrying the child exit code (``rc`` is
    ``None`` only if the wrapper crashed before ``proc.wait()`` could run) and,
    when the stall watchdog aborted the build, the wedged task labels. Does not
    do step logging, warn/err printing, PSI calibration, or sampler management -
    the caller owns those.
    """
    # Deferred: avoids a module-level import cycle with kas_build (which
    # imports this module to re-export _run_pty_with_ui), and neither name is
    # otherwise imported into this module's namespace.
    from bakar.steps import kas_build

    # Unpacked back into identically-named locals on purpose: nine inner
    # closures below (the pump, the heartbeat, the event tail, the stall and
    # error watchdogs, ...) read these as free variables. Rebinding them here
    # keeps every closure body untouched by the repack.
    cmd = ctx.cmd
    cfg = ctx.cfg
    log = ctx.log
    ui = ctx.ui
    stop_event = ctx.stop_event
    show_layers = ctx.show_layers
    output_mode = ctx.output_mode
    scope_unit = ctx.scope_unit
    rc: int | None = None
    stall_tasks: list[str] | None = None
    # Per-build cache delta for the build-end summary, filled at teardown.
    cache_backend: str | None = None
    cache_doc: dict | None = None
    # Declared up here so the finally can close the run's journal record even if
    # setup raised before the emitter was built.
    emitter: journal.JournalEmitter | None = None
    master_fd, slave_fd = pty.openpty()  # pragma: no cover
    try:
        with log.kas_log_path.open("w", encoding="utf-8", buffering=1) as kas_log:
            proc = subprocess.Popen(  # pragma: no cover
                cmd,
                cwd=cfg.bsp_root,
                # stdin must be a TTY too: kas-container sees stdout as a
                # TTY (via slave_fd) and passes -t -i to docker, which
                # then requires stdin to also be a TTY or it refuses with
                # "cannot attach stdin to a TTY-enabled container
                # because stdin is not a terminal". Sharing the same pty
                # slave across stdin/stdout/stderr satisfies that check.
                # We never write to master_fd, so the child's stdin reads
                # block indefinitely - which is fine for a batch build.
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                # scope_env re-adds the user-bus vars systemd-run needs (a no-op
                # for an unscoped launch); the curated _build_env otherwise omits
                # them. See bakar.build_scope.scope_env.
                env=build_scope.scope_env(
                    kas_build._build_env(cfg, eventlog_path=kas_build._container_eventlog_path(cfg, log)), cfg
                ),
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            # Persist the build PGID so `bakar stop` can target this run.
            # proc.pid is the PGID because start_new_session=True above makes
            # the child a process-group leader.
            build_stop.write_launch_record(
                log.run_dir,
                pgid=proc.pid,
                mode=("host" if cfg.host_mode else "container"),
                runtime=(None if cfg.host_mode else build_stop._detect_runtime()),
                container_label=(None if cfg.host_mode else build_stop.run_id_label(log.run_id)),
            )

            live_frozen = False
            # Rich's Live.stop() sets vertical_overflow="visible" so its final
            # frame renders uncropped, and never puts it back. The freeze below
            # restarts the Live afterwards, so the value has to be carried
            # across the stop by hand - see the restore at the restart site.
            frozen_overflow = "ellipsis"

            def _process_line(line: str) -> None:  # pragma: no cover
                nonlocal live_frozen, frozen_overflow
                kas_log.write(line + "\n")
                kas_log.flush()
                msg = ui.process_line(line)
                # Failure freeze: stop the Live BEFORE printing the first
                # error line of a task failure, committing the collapsed
                # frame (pipeline, sstate, failure count) into the
                # scrollback above the failure text about to stream.
                if not live_frozen and ui.take_fail_freeze():
                    frozen_overflow = live.vertical_overflow
                    live.stop()
                    live_frozen = True
                    # Emitted at the freeze rather than at build end so the
                    # failure is timestamped when it happened - a stop_on_error
                    # build keeps running for minutes afterwards while already
                    # -started tasks drain.
                    if emitter is not None:
                        report = ui.journal_report()
                        emitter.send(
                            "task_failed",
                            f"task failed: {report.get('first_failure', 'unknown')}",
                            priority=journal.PRIORITY_ERROR,
                            **report,
                        )
                if msg:
                    live.console.print(msg)
                info = ui.take_pending_log()
                if info:
                    log.info(info)
                alerts = ui.take_pending_alerts()
                for alert in alerts:
                    live.console.print(alert)
                # Resume the Live once the failure context has fully landed:
                # after the TaskFailed alert block (event feed), or on the
                # next task-counter line (regex fallback, where no event
                # will arrive).
                if live_frozen and (alerts or ui.take_pending_restart()):
                    live.start(refresh=True)
                    # Undo Live.stop()'s one-way flip to "visible". Left alone,
                    # every later frame renders uncropped, so Rich's cursor-up
                    # erase is sized for a panel taller than the terminal,
                    # overshoots, and each refresh stacks a fresh copy of the
                    # panel instead of redrawing in place.
                    live.vertical_overflow = frozen_overflow
                    live_frozen = False
                    ui.notify_restarted()

            def _pump() -> None:  # pragma: no cover
                buf = b""
                while True:
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        # EIO fires on Linux when the slave side closes
                        # (child exited). Treat as EOF.
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        m = LINE_SPLIT_RE.search(buf)
                        if m is None:
                            break
                        raw = buf[: m.start()]
                        buf = buf[m.end() :]
                        if not raw:
                            continue
                        line = _strip_ansi(raw.decode("utf-8", errors="replace"))
                        _process_line(line)
                if buf:
                    tail = _strip_ansi(buf.decode("utf-8", errors="replace"))
                    if tail:
                        _process_line(tail)

            # One-shot layer display: kas materializes bblayers.conf early in
            # the build (manifest paths have it even earlier, from setup-env),
            # so the heartbeat polls for it and prints the panel above the
            # live region as soon as the data exists - at the START of the
            # build, where it is useful, instead of after it finishes.
            layers_pending = show_layers

            def _heartbeat() -> None:
                nonlocal layers_pending
                while not stop_event.wait(timeout=1):
                    if proc.poll() is not None:
                        break
                    if layers_pending:  # pragma: no cover - PTY-thread path
                        from bakar.layers import collect_layer_hashes, layer_hash_table

                        hashes = collect_layer_hashes(cfg)
                        if hashes:
                            live.console.print(layer_hash_table(hashes))
                            layers_pending = False

            event_feed_count = 0
            event_feed_error = ""

            def _event_tail() -> None:  # pragma: no cover
                # Authoritative feed: drive the live model from bitbake's
                # structured event log. ui.process_line (regex) stays as the
                # degraded fallback. A tailer error must never crash the
                # build, but it must not die silently either - the count and
                # error are reported after the build so a dead feed (live UI
                # quietly running on the regex fallback) is diagnosable.
                nonlocal event_feed_count, event_feed_error
                try:
                    for class_name, event in tail_events(log.eventlog_path, stop_event):
                        ui.process_event(class_name, event)
                        event_feed_count += 1
                except Exception as exc:  # noqa: BLE001 - event feed errors are captured and reported; must not crash the build thread
                    event_feed_error = f"{type(exc).__name__}: {exc}"

            def _stall_watchdog() -> None:  # pragma: no cover
                # Self-guard against a wedged task (e.g. a deadlocked final
                # link): when every running task's log has been silent past
                # cfg.stall_abort_secs, SIGINT the build so it fails cleanly
                # naming the stuck task instead of spinning until the user
                # Ctrl-C's. bitbake's own keepalive output flows through the
                # PTY pump, so raw output cannot be the signal - log freshness
                # is what distinguishes a wedge from a slow-but-alive compile.
                nonlocal stall_tasks
                if cfg.stall_abort_secs <= 0:
                    return
                while not stop_event.wait(timeout=_STALL_POLL_SECS):
                    if proc.poll() is not None:
                        break
                    report = ui.stall_report()
                    if report is None:
                        continue
                    stalled, labels = report
                    if stalled >= cfg.stall_abort_secs:
                        stall_tasks = labels
                        log.warn(
                            f"build stalled: no log output for {_fmt_stall(stalled)} from running "
                            f"task(s) {', '.join(labels)}; aborting. Disable with "
                            "`bakar settings set build.stall_abort_secs 0`."
                        )
                        build_stop.stop_running_proc(proc, cfg, log)
                        break

            def _error_watchdog() -> None:  # pragma: no cover
                # SIGINT the build the moment any task fails, instead of
                # waiting for bitbake's own halt-on-failure to drain every
                # already-running task on its own schedule. bitbake already
                # stops scheduling *new* tasks the instant a task fails
                # regardless of this setting - this only stops bakar's live
                # view from rendering a misleadingly-normal progress display
                # while it waits for tasks that started before the failure
                # (which can run for a long time) to finish on their own.
                if not cfg.stop_on_error:
                    return
                while not stop_event.wait(timeout=_ERROR_POLL_SECS):
                    if proc.poll() is not None:
                        break
                    if ui.had_task_failures:
                        log.warn(
                            "build failed: a task reported failure; aborting immediately. "
                            "Disable with `bakar settings set build.stop_on_error false`."
                        )
                        build_stop.stop_running_proc(proc, cfg, log)
                        break

            # Holds the freshest daemon_doc/ccache_doc the cache-probe thread
            # computed, so the build-end persist reuses that probe rather than
            # issuing a second one after the build completes. The ``first_*``
            # holders snapshot the FIRST SUCCESSFUL PROBE from the cache-probe
            # thread (see ``_cache_probe`` -> ``_refresh`` below), NOT build
            # start: the thread's initial ``_refresh()`` call races the build
            # process and can fail (daemon/cache not up yet), in which case the
            # holder stays None until a later iteration succeeds. Any cache
            # activity between build start and that first successful probe is
            # therefore excluded from the build-end delta (``cache_delta``
            # below). This is a deliberate tradeoff, not a bug: closing the gap
            # would require a synchronous pre-loop baseline snapshot, which is
            # riskier than the narrow accuracy gap it leaves. In the degenerate
            # case where the probe only ever succeeds once (first == last), the
            # delta is honestly all-zero - not wrong, just narrow.
            last_daemon_doc: list = [None]
            first_daemon_doc: list = [None]
            last_ccache_doc: list = [None]
            first_ccache_doc: list = [None]

            # Structured milestones only - the build's output stays in kas.log.
            # scope_unit is the join key: it lets a reader line these records up
            # against systemd's own "Consumed ... CPU time" / OOM / memory-peak
            # lines for the same unit, which is the correlation a log file cannot
            # provide. Created after the cache-doc holders above so the build_end
            # record in the finally can always read them.
            emitter = journal.JournalEmitter(
                {
                    "run_id": log.run_id,
                    "machine": cfg.machine or "",
                    "workspace": str(cfg.bsp_root),
                    **({"scope_unit": scope_unit} if scope_unit else {}),
                },
                enabled=cfg.journal,
            )
            emitter.send(
                "build_start",
                f"build started: machine={cfg.machine} workspace={cfg.bsp_root}",
                **journal.health_fields(cfg.resolved_tmpdir),
            )

            def _journal_progress() -> None:  # pragma: no cover - timing-driven daemon thread
                """Emit one full progress snapshot per ``journal_interval`` tick.

                Level-sampled rather than event-driven on purpose: the value is a
                record that keeps arriving on a predictable cadence, so a run that
                stopped moving shows up as an unchanged snapshot instead of as
                silence, which is indistinguishable from a finished build.
                """
                if not emitter.enabled:
                    return
                while not stop_event.wait(timeout=max(1, cfg.journal_interval)):
                    report = ui.journal_report()
                    emitter.send(
                        "progress",
                        "build progress: " + " ".join(f"{k}={v}" for k, v in report.items()),
                        **report,
                        **journal.health_fields(cfg.resolved_tmpdir),
                        **journal.cache_fields(last_daemon_doc[0], last_ccache_doc[0]),
                    )

            def _cache_probe() -> None:  # pragma: no cover
                # Refresh the cluster/cache header lines shown in the build UI.
                # sccache-dist builds show the cluster + sccache daemon lines;
                # ccache builds show a single ccache hit/miss line. No-op when
                # neither cache launcher is active.
                if not (cfg.use_sccache_dist or cfg.ccache):
                    return

                def _refresh() -> None:
                    # Best-effort cosmetic probe: a failure here must never crash
                    # or spew from this daemon thread, so swallow everything (the
                    # probes are never-raise in production; this guards the test
                    # harness and any unforeseen edge).
                    try:
                        if cfg.use_sccache_dist:
                            cluster = probe_cluster(cfg.sccache_scheduler_url)
                            daemon = probe_build_daemon()
                            lines = render_cluster(cluster_doc(cluster, cfg.sccache_scheduler_url))
                            doc = daemon_doc(daemon) if daemon.running else None
                            if doc is not None:
                                last_daemon_doc[0] = doc
                                if first_daemon_doc[0] is None:
                                    first_daemon_doc[0] = doc
                                # Live badge is status, not accounting: cumulative
                                # so-far hit rate plus the current daemon verdict.
                                ui.set_cache_badge(
                                    active=True,
                                    hit_pct=cache_hit_pct(doc["cache_hits"], doc["cache_misses"]),
                                    verdict=doc["verdict"],
                                )
                            lines.append(render_sccache_cache(doc))
                        else:
                            cc = probe_ccache(cfg.effective_ccache_dir)
                            cc_doc = ccache_doc(cc)
                            if cc_doc is not None:
                                last_ccache_doc[0] = cc_doc
                                if first_ccache_doc[0] is None:
                                    first_ccache_doc[0] = cc_doc
                                # ccache has no distribution: cache badge only,
                                # no verdict (suppresses the dist badge/token).
                                ui.set_cache_badge(active=True, hit_pct=cc_doc["hit_rate"], verdict=None)
                            lines = [render_ccache_cache(cc_doc)]
                        ui.set_dist_lines(lines)
                    except Exception:  # noqa: BLE001 - cosmetic probe, never crash the build thread
                        return

                _refresh()  # show immediately
                while not stop_event.wait(timeout=3):
                    _refresh()

            # Share the run logger's console so log.info() (the parse-complete
            # line) coordinates with the live region instead of printing onto
            # the same line as the setup bar.
            frame_cm: Live | _PlainFrameController = (
                _PlainFrameController(ui, log.console, stop_event)
                if output_mode is OutputMode.PLAIN
                else Live(get_renderable=ui.make_renderable, console=log.console, refresh_per_second=8)
            )
            with frame_cm as live:
                pump = threading.Thread(target=_pump, daemon=True)  # pragma: no cover
                pump.start()
                heartbeat = threading.Thread(target=_heartbeat, daemon=True)  # pragma: no cover
                heartbeat.start()
                event_tail = threading.Thread(target=_event_tail, daemon=True)  # pragma: no cover
                event_tail.start()
                watchdog = threading.Thread(target=_stall_watchdog, daemon=True)  # pragma: no cover
                watchdog.start()
                error_watchdog = threading.Thread(target=_error_watchdog, daemon=True)  # pragma: no cover
                error_watchdog.start()
                cache_probe = threading.Thread(target=_cache_probe, daemon=True)  # pragma: no cover
                cache_probe.start()
                journal_progress = threading.Thread(target=_journal_progress, daemon=True)  # pragma: no cover
                journal_progress.start()
                try:
                    rc = proc.wait()
                except KeyboardInterrupt:
                    build_stop.stop_running_proc(proc, cfg, log)
                    rc = proc.wait()
                stop_event.set()
                pump.join(timeout=5)
                heartbeat.join(timeout=2)
                watchdog.join(timeout=2)
                error_watchdog.join(timeout=2)
                if layers_pending:  # pragma: no cover - fast build finished before first heartbeat tick
                    from bakar.layers import collect_layer_hashes, layer_hash_table

                    hashes = collect_layer_hashes(cfg)
                    if hashes:
                        live.console.print(layer_hash_table(hashes))
                        layers_pending = False
                event_tail.join(timeout=5)
                # Join the cache probe (the one teardown thread not joined
                # above) so the last_* holders are current before we read them.
                cache_probe.join(timeout=1)
                # Persist this-build cache deltas (the raw counters are
                # cumulative odometers). Persist the DELTA, not the lifetime
                # total, for whichever backend was active; the probe branches are
                # mutually exclusive so exactly one artifact is written. Compute
                # the summary doc here (inside the block, where the holders are
                # read) but PRINT it at the post-block site. Best-effort: a
                # persistence failure must never crash a completed build.
                try:
                    sccache_delta = cache_delta(first_daemon_doc[0], last_daemon_doc[0])
                    ccache_delta = cache_delta(first_ccache_doc[0], last_ccache_doc[0])
                    log.persist_sccache_stats(sccache_delta)
                    log.persist_ccache_stats(ccache_delta)
                    if sccache_delta is not None:
                        cache_backend, cache_doc = "sccache", sccache_delta
                    elif ccache_delta is not None:
                        cache_backend, cache_doc = "ccache", ccache_delta
                except Exception as exc:  # noqa: BLE001 - best-effort; never crash the build
                    log.warn(f"failed to persist cache stats: {exc}")
                if event_feed_error:
                    log.warn(f"bitbake event feed died ({event_feed_error}); live UI ran on regex fallback")
                elif event_feed_count == 0:
                    log.warn(
                        f"bitbake event feed inactive (0 events from {log.eventlog_path}); "
                        "live UI ran on regex fallback"
                    )
                if rc == 0:
                    # Freeze the final frame with every reached pipeline
                    # segment checked (Live renders once more on exit);
                    # without this the header ends on a spinner forever.
                    ui.finish()
                elif ui.had_task_failures:
                    # Each failure's pipeline status and context already
                    # committed inline (frozen frame + alert block);
                    # repeating the frame here would wedge it between the
                    # failure text and the runner's exit lines. No-op when
                    # the Live is still frozen (already out of the way).
                    live.transient = True
                else:
                    # Failed without a recorded task failure (parse abort,
                    # container error): keep a collapsed closing status.
                    ui.finish_failed()
    finally:
        # In the finally so an aborted or crashed run still closes its record;
        # a start with no end is exactly the shape that makes a journal timeline
        # unreadable. emitter is absent only if setup raised before it existed.
        if emitter is not None:
            report = ui.journal_report()
            emitter.send(
                "build_end",
                f"build finished: rc={rc} " + " ".join(f"{k}={v}" for k, v in report.items()),
                priority=journal.PRIORITY_INFO if rc == 0 else journal.PRIORITY_WARNING,
                rc=rc,
                **report,
                **journal.cache_fields(last_daemon_doc[0], last_ccache_doc[0]),
            )
            emitter.close()
        build_stop.remove_pid(log.run_dir)
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
    return _PtyOutcome(rc=rc, stall_tasks=stall_tasks, cache_backend=cache_backend, cache_doc=cache_doc)
