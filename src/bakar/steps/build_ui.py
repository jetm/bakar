"""Rich Live UI state for the kas-container build progress display.

Parses bitbake knotty's **non-interactive fallback** output lines and renders a
phase-aware Rich Live display:

- SETUP phase (loading cache / parsing recipes) renders a single percentage bar.
  When parsing completes, a one-time "parsing recipes complete (Ns)" message is
  queued for the caller to drain via ``take_pending_log`` and emit through its
  own logger, so it gets the same INFO tag as the other run-log lines.
- BUILD phase (setscene reuse + task execution) renders the X-of-Y counter bar --
  which ends with the global wall-clock timer (a Nerd-Font timer icon plus Rich's
  elapsed column; it starts at construction, so it includes parse time and never
  resets) -- followed by a live per-task table: a braille spinner, a nerd-font
  task-type icon, the recipe (PF), the task name, and elapsed time. Rows are sorted
  by elapsed descending and colored by task type; the elapsed cell turns yellow/red
  when a task runs far longer than the median (stuck-task detection).

The transition to BUILD happens on the first ``Running [setscene] task N of M``
line. The running-task set is reconstructed from ``recipe PF: task T: Started``
and ``: Succeeded``/``: Failed`` lifecycle events, keyed on ``PF:task``, with
elapsed computed from the local monotonic clock.

The display assumes a truecolor terminal and a Nerd Font (the icons live in the
Font Awesome private-use range). A CI/plain mode that degrades the glyphs and
color is planned separately.

``BuildUIState`` is console-agnostic: ``process_line`` returns a passthrough
string for severity lines and ``None`` otherwise so the caller can forward
severity messages to ``live.console.print()`` without coupling this class to any
console or Live instance.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from bakar import cache_render, task_timings
from bakar.eventlog import (
    _CACHE_BACKEND_EVENT_TYPE,
    _METADATA_EVENT,
    _RUNQUEUE_TASK_STARTED,
    _TASK_FAILED,
    _TASK_FAILED_SILENT,
    _TASK_STARTED,
    _TASK_SUCCEEDED,
    _stat,
    _task_key,
)
from bakar.fmt import fmt_duration

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bakar.eventlog import _EventStub

# bitbake event class names (the decoded line's ``class`` field) the live feed
# consumes. Mirrors eventlog.py's naming; classified by string, not isinstance.
_EVT_PARSE_PROGRESS = "bb.event.ParseProgress"
_EVT_CACHE_LOAD_PROGRESS = "bb.event.CacheLoadProgress"
_EVT_PARSE_COMPLETED = "bb.event.ParseCompleted"
_EVT_RUNQUEUE_TASK_STARTED = _RUNQUEUE_TASK_STARTED
_EVT_TASK_STARTED = _TASK_STARTED
_EVT_TASK_SUCCEEDED = _TASK_SUCCEEDED
_EVT_TASK_FAILED = _TASK_FAILED
_EVT_TASK_FAILED_SILENT = _TASK_FAILED_SILENT
_EVT_SCENE_TASK_STARTED = "bb.runqueue.sceneQueueTaskStarted"
_EVT_SCENE_TASK_COMPLETED = "bb.runqueue.sceneQueueTaskCompleted"
_EVT_SCENE_QUEUE_COMPLETE = "bb.runqueue.sceneQueueComplete"
_EVT_SCENE_TASK_FAILED = "bb.runqueue.sceneQueueTaskFailed"
_EVT_RUNQUEUE_TASK_COMPLETED = "bb.runqueue.runQueueTaskCompleted"
_EVT_RUNQUEUE_TASK_FAILED_RQ = "bb.runqueue.runQueueTaskFailed"
_EVT_RUNQUEUE_TASK_SKIPPED = "bb.runqueue.runQueueTaskSkipped"

# Knotty fallback line formats (non-interactive mode inside the kas container).
LOADING_CACHE = re.compile(r"Loading cache:\s+(\d+)%")
PARSE_PROGRESS = re.compile(r"Parsing recipes:\s+(\d+)%")
RUNNING_TASK = re.compile(r"Running (setscene )?task (\d+) of (\d+)")  # g1=setscene?, g2=N, g3=M
RECIPE_STARTED = re.compile(r"recipe (\S+): task (do_\S+): Started")
RECIPE_DONE = re.compile(r"recipe (\S+): task (do_\S+): (?:Succeeded|Failed)")
FALLBACK_MODE = re.compile(r"Unable to use interactive mode")

# Lines to surface above the Live display so users see real problems.
SEVERITY_PASSTHROUGH = re.compile(r"\b(ERROR|FATAL|WARNING|QA Issue):")

# First knotty line of a task-failure report ("ERROR: <PF> <task>: ...").
# Detecting it on the PTY feed - BEFORE the line prints - is the only way
# to commit the live frame above the failure text: the structured
# TaskFailed event arrives later on the event-log tailer, when the error
# block has already hit the terminal and lines cannot be reordered.
TASK_FAIL_HEAD = re.compile(r"^ERROR: (\S+) (do_\S+): ")

# Braille spinner frames (universal Unicode, not Nerd-Font dependent).
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Cap on rendered task-table rows so the Live region fits the terminal even
# on high-core hosts (BB_NUMBER_THREADS rows otherwise). Longest-elapsed
# tasks are kept; the remainder collapses into a "+N more running" line.
_MAX_TASK_ROWS = 16

# Nerd-Font icons (Font Awesome private-use range, confirmed present in
# IosevkaTerm / CommitMono Nerd Font). Truecolor + Nerd Font assumed.
_ICON_COMPILE = ""  # cog
_ICON_FETCH = ""  # download
_ICON_CONFIGURE = ""  # wrench
_ICON_PACKAGE = ""  # package
_ICON_SETSCENE = ""  # fa-refresh
_ICON_TIMER = "󰦗"  # md-progress-clock (build elapsed)
_ICON_DRIFT = ""  # fa-warning (stuck task: time drifted past its reference)


class _Phase(Enum):
    SETUP = "setup"
    BUILD = "build"


@dataclass(slots=True)
class _RunTask:
    pf: str
    task: str
    start: float  # time.monotonic() at Started
    estimated: float | None = None  # historical mean seconds for this taskname, if known
    logfile: str | None = None  # host path to the task's log, when known (for the stall guard)
    cache_backend: str | None = None  # sstate/from-scratch/hashequiv classification, when known


def _fmt_stall(seconds: int) -> str:
    return fmt_duration(seconds)


def _task_style(task: str) -> tuple[str, str]:
    """Return (nerd-font icon, color) for a bitbake task name."""
    if task.endswith("_setscene"):
        return (_ICON_SETSCENE, "bright_black")
    if "compile" in task:
        return (_ICON_COMPILE, "yellow")
    if "fetch" in task or "unpack" in task or "mirror" in task:
        return (_ICON_FETCH, "blue")
    if "configure" in task or "patch" in task or "prepare" in task or "cmake" in task:
        return (_ICON_CONFIGURE, "cyan")
    if any(k in task for k in ("package", "deploy", "image", "rootfs", "spdx", "install", "populate")):
        return (_ICON_PACKAGE, "green")
    return (_ICON_COMPILE, "white")


def _stuck_color(elapsed: float, median: float, count: int, estimated: float | None = None) -> str | None:
    """Return a highlight color when a task runs far longer than expected, else None.

    When ``estimated`` (the task's historical mean) is available, compare against
    it directly -- it is a stable reference even early in a build, so the
    ``count``/``median`` guard the run-local path needs does not apply. When
    ``estimated`` is None, fall back to the current-run median.
    """
    if estimated is not None and estimated > 0:
        if elapsed > 4 * estimated:
            return "bold red"
        if elapsed > 2 * estimated:
            return "yellow"
        return None
    if count < 3 or median <= 0:
        return None
    if elapsed > 4 * median:
        return "bold red"
    if elapsed > 2 * median:
        return "yellow"
    return None


class BuildUIState:
    """Mutable UI state that parses knotty fallback lines and renders a Rich Live display.

    Construct the two ``Progress`` objects as plain objects -- do NOT use them as
    context managers. The outer ``Live(get_renderable=ui.make_renderable)`` owns
    the terminal refresh loop; entering ``Progress`` as a context manager would
    start a second internal ``Live`` and corrupt the cursor.

    Thread safety: ``_running`` mutations are guarded by ``_lock``. ``process_line``
    may be called from the pump/heartbeat threads; ``make_renderable`` is called
    from the ``Live`` refresh thread.
    """

    def __init__(
        self,
        start_monotonic: float | None = None,
        logfile_translator: Callable[[str], str] | None = None,
        timings_path: Path | None = None,
        *,
        show_baseline_drift: bool = False,
    ) -> None:
        """``start_monotonic`` is the ``time.monotonic()`` stamp of when ``bakar``
        started (RunLogger captures it before doctor). When given, the global
        timer on the pipeline header counts from there -- including doctor,
        sync, and parse -- instead of from this object's construction.

        ``timings_path`` selects the context-scoped baseline file (see
        ``task_timings.timings_path_for``); ``None`` falls back to the legacy
        global file.

        ``show_baseline_drift`` gates whether historical baselines are loaded
        at all. When ``False`` (the default), ``_task_baselines`` stays an
        empty dict regardless of ``timings_path`` -- estimated durations and
        stuck-task drift detection fall back to the current-run median.
        """
        # The global wall-clock timer lives on the pipeline header (the
        # parse -> setscene -> build line), not on the bars, so it is in the
        # same place in every phase and never resets across transitions.
        self._start_monotonic = start_monotonic if start_monotonic is not None else time.monotonic()

        self._setup_progress = Progress(
            TextColumn("[cyan]{task.fields[stage]}[/]"),
            BarColumn(style="grey30", complete_style="cyan", finished_style="green"),
            TextColumn("{task.percentage:>3.0f}%"),
        )
        self._setup_task_id = self._setup_progress.add_task("setup", total=100, stage="starting")

        self._build_progress = Progress(
            TextColumn("[cyan]kas_build[/]"),
            BarColumn(style="grey30", complete_style="cyan", finished_style="green"),
            TextColumn("{task.completed}/{task.total} {task.fields[kind]}"),
        )
        self._build_task_id = self._build_progress.add_task("build", total=None, kind="")

        self._phase = _Phase.SETUP
        self._stage = "starting"
        self._kind = ""
        self._running: dict[str, _RunTask] = {}
        self._lock = threading.Lock()
        # Setscene-reuse counters from runQueueTaskStarted.stats; rendered as a
        # "from sstate cache" line only when _setscene_total > 0.
        self._setscene_covered = 0
        self._setscene_total = 0
        self._setscene_notcovered = 0
        # Flipped True on the first process_event call. Once set, process_line
        # stops mutating the progress model (the event feed owns it) but still
        # does fallback detection, severity passthrough, and warn/error counts.
        self._event_driven = False
        self.fallback_detected = False
        self._frame = 0
        # Stamped on the first "Parsing recipes" line so the parse->build
        # transition can report how long parsing took.
        self._parse_start: float | None = None
        # One-shot message the caller drains via take_pending_log() and emits
        # through its own logger, so it renders like the other run-log lines.
        self._pending_log: str | None = None
        self.warn_count: int = 0
        self.error_count: int = 0
        self._logfile_translator = logfile_translator
        # Persistent record of (recipe, taskname) for each failed task,
        # rendered in the build bar. ``_pending_alerts`` queues one-shot
        # alert blocks (✗ FAILED line, host log path, log tail) the caller
        # drains via take_pending_alerts() and prints below the frozen frame.
        self._failures: list[tuple[str, str]] = []
        self._pending_alerts: list[RenderableType] = []
        # Cluster/cache header lines shown during a build, fed by a probe thread
        # via set_dist_lines() and injected into the normal building frame.
        self._dist_lines: list[RenderableType] = []
        # At-a-glance cache/dist badge state, fed by the cache-probe thread via
        # set_cache_badge() and read by make_renderable/plain_status_line. Guarded
        # by _lock (like _dist_lines): a daemon thread writes, render threads read.
        # ``_cache_badge_active`` gates emission; ``_dist_verdict`` is set only for
        # an sccache daemon (a ccache build leaves it None -> no dist badge/token).
        self._cache_badge_active: bool = False
        self._cache_hit_pct: float = 0.0
        self._dist_verdict: str | None = None
        self._task_failed_count: int = 0
        # Freeze protocol: the first knotty error line of a task failure
        # (TASK_FAIL_HEAD) sets ``_pending_freeze``; the build runner drains
        # it via take_fail_freeze() and stops the Live BEFORE printing that
        # line, committing the collapsed frame (pipeline, sstate, failure
        # count) into the scrollback above the failure text. While
        # ``_fail_frozen`` the failure text streams as plain prints; the
        # runner restarts the Live after the TaskFailed alert lands (or on
        # the next Running-task line in regex-fallback mode, drained via
        # take_pending_restart) and calls notify_restarted().
        self._fail_frozen = False
        self._pending_freeze = False
        self._pending_restart = False
        # High-water task-table column widths (recipe, task, elapsed). Grow
        # to the longest cell seen this run and never shrink, so columns hold
        # a static position frame-to-frame instead of jittering as the set of
        # running tasks changes.
        self._w_pf = 0
        self._w_task = 0
        self._w_elapsed = 0
        # Per-stage wall-clock durations, rendered next to each completed
        # pipeline segment ("✓ parse (51s)"). Keyed "parse"/"setscene"/
        # "tasks". parse closes at ParseCompleted, setscene at bitbake's
        # sceneQueueComplete event (the scene queue draining - NOT the first
        # real task, since the two interleave), tasks at finish().
        # ``_finished`` freezes the breadcrumb with every reached segment
        # checked - without it the final Live frame keeps a spinner on
        # whatever stage was current when the build exited.
        self._seg_durations: dict[str, int] = {}
        self._scene_started_at: float | None = None
        self._scene_done_at: float | None = None
        self._tasks_started_at: float | None = None
        self._finished = False
        # Set by finish_failed(): the final frame collapses to the pipeline
        # header and sstate line. The failure list and log tails print once
        # via failure_report() after the region closes, so repeating them
        # in the frozen frame would duplicate.
        self._failed_final = False
        # Historical timing baselines {"recipe:task": (mean, stddev)} from
        # prior builds of THIS context (workspace+machine+mode). Empty on the
        # first build -> estimated stays None and stuck-task detection falls
        # back to the current-run median.
        self._task_baselines = task_timings.load_baselines(timings_path) if show_baseline_drift else {}
        # Last line returned by plain_status_line(); enables idle-dedup in plain mode
        # (the emission RATE is bounded by the caller thread's tick, not by this class).
        self._plain_last_line: str | None = None

    def process_line(self, line: str) -> str | None:
        """Parse one line of knotty fallback output and update internal state.

        Returns a Rich markup string for severity lines so the caller can
        forward it to ``live.console.print()``. Returns ``None`` for all
        other lines.
        """
        # 1. Mode detection: record that the fallback parser path is active.
        # This runs regardless of feed source so degraded-mode reporting holds.
        if FALLBACK_MODE.search(line):
            self.fallback_detected = True
            return None

        # Once the event feed is driving the model, the regex feed stops
        # mutating parse/cache/build progress -- the structured stream owns it.
        # Severity passthrough and warn/error counting below still run.
        with self._lock:
            event_driven = self._event_driven
        if not event_driven:
            # 2. Parse phase progress. A parse line while already building marks
            # a second bitbake invocation (rebuild's cleansstate && build).
            m = PARSE_PROGRESS.search(line)
            if m:
                if self._phase is _Phase.BUILD:
                    self._reset_for_new_build_cycle()
                self._stage = "parsing recipes"
                if self._parse_start is None:
                    self._parse_start = time.monotonic()
                self._setup_progress.update(self._setup_task_id, completed=int(m.group(1)), stage=self._stage)
                return None

            # 3. Cache load progress.
            m = LOADING_CACHE.search(line)
            if m:
                if self._phase is _Phase.BUILD:
                    self._reset_for_new_build_cycle()
                self._stage = "loading cache"
                self._setup_progress.update(self._setup_task_id, completed=int(m.group(1)), stage=self._stage)
                return None

            # 4. Task counter -- flips to the BUILD phase.
            m = RUNNING_TASK.search(line)
            if m:
                with self._lock:
                    entering_build = self._phase is _Phase.SETUP
                    self._phase = _Phase.BUILD
                    # Regex-fallback restart path: no TaskFailed event will
                    # arrive to end a failure freeze, so the next task
                    # counter line signals the runner to resume the Live.
                    if self._fail_frozen:
                        self._pending_restart = True
                    # Degraded-mode stage stamps: no sceneQueueComplete is
                    # observable from knotty lines, so setscene only closes
                    # at finish(); the segments still advance.
                    if m.group(1):
                        if self._scene_started_at is None:
                            self._scene_started_at = time.monotonic()
                    elif self._tasks_started_at is None:
                        self._tasks_started_at = time.monotonic()
                self._kind = "setscene" if m.group(1) else "tasks"
                completed, total = int(m.group(2)), int(m.group(3))
                self._build_progress.update(
                    self._build_task_id,
                    completed=completed,
                    total=total,
                    kind=self._kind,
                )
                # Announce parse completion once. Queue it for the caller to emit
                # through its logger (so it gets the same INFO tag as other run-log
                # lines), reporting how long parsing took. Also close the parse
                # segment's clock - this branch can fire before (or instead of)
                # the ParseCompleted event when the regex feed wins the race,
                # and the breadcrumb's "✓ parse (51s)" reads the stored value.
                if entering_build:
                    took = ""
                    if self._parse_start is not None:
                        with self._lock:
                            self._seg_durations.setdefault("parse", int(time.monotonic() - self._parse_start))
                            took = f" ({_fmt_stall(self._seg_durations['parse'])})"
                    self._pending_log = f"[green]✓[/] parsing recipes complete{took}"
                return None

            # 5. Task started -- add to the running set.
            m = RECIPE_STARTED.search(line)
            if m:
                key = f"{m.group(1)}:{m.group(2)}"
                with self._lock:
                    self._running[key] = _RunTask(pf=m.group(1), task=m.group(2), start=time.monotonic())
                return None

            # 6. Task done -- remove from the running set.
            m = RECIPE_DONE.search(line)
            if m:
                key = f"{m.group(1)}:{m.group(2)}"
                with self._lock:
                    self._running.pop(key, None)
                return None

        # 7. Severity lines surface above the Live display.
        m = SEVERITY_PASSTHROUGH.search(line)
        if m:
            token = m.group(1)
            if token == "WARNING":
                self.warn_count += 1
            elif token in ("ERROR", "FATAL"):
                self.error_count += 1
                # First error line of a task failure: record the failure
                # and request a freeze so the runner commits the live frame
                # above this line. Dedupe on (PF, task) - bitbake emits
                # several "ERROR: <PF> <task>: ..." lines per failure.
                head = TASK_FAIL_HEAD.match(line)
                if head:
                    key = (head.group(1), head.group(2))
                    with self._lock:
                        if key not in self._failures:
                            self._failures.append(key)
                            self._task_failed_count += 1
                            self._fail_frozen = True
                            self._pending_freeze = True
            return line

        # 8. Default.
        return None

    def process_event(self, class_name: str, event: _EventStub) -> None:
        """Update the model from one decoded bitbake event.

        ``class_name`` is the event-log line's ``class`` string; ``event`` is the
        decoded :class:`bakar.eventlog._EventStub` (missing attributes read as
        ``None``). The render model is input-agnostic, so this drives the same
        ``_setup_progress``/``_build_progress``/``_running`` state the regex feed
        does. The first call flips ``_event_driven`` True so ``process_line``
        stops mutating the model.
        """
        with self._lock:
            self._event_driven = True

        if class_name == _EVT_PARSE_PROGRESS:
            self._update_setup(event, "parsing recipes")
            return

        if class_name == _EVT_CACHE_LOAD_PROGRESS:
            self._update_setup(event, "loading cache")
            return

        if class_name == _EVT_PARSE_COMPLETED:
            with self._lock:
                took = ""
                if self._parse_start is not None:
                    # setdefault: the regex fallback may have already closed
                    # the parse clock (it can win the race against the event
                    # tailer); a late replayed event must not inflate it.
                    self._seg_durations.setdefault("parse", int(time.monotonic() - self._parse_start))
                    took = f" ({_fmt_stall(self._seg_durations['parse'])})"
                cached = getattr(event, "cached", None) or 0
                parsed_count = getattr(event, "parsed", None) or 0
                if cached + parsed_count > 0:
                    if parsed_count == 0:
                        cache_note = "  [all from cache]"
                    elif cached == 0:
                        cache_note = f"  [{parsed_count} fresh, cache empty]"
                    else:
                        cache_note = f"  [{int(cached / (cached + parsed_count) * 100)}% cached, {parsed_count} fresh]"
                else:
                    cache_note = ""
                self._pending_log = f"[green]✓[/] parsing recipes complete{took}{cache_note}"
            return

        if class_name == _EVT_RUNQUEUE_TASK_STARTED:
            self._update_build(event)
            return

        if class_name in (_EVT_RUNQUEUE_TASK_COMPLETED, _EVT_RUNQUEUE_TASK_FAILED_RQ, _EVT_RUNQUEUE_TASK_SKIPPED):
            self._update_build(event)
            return

        if class_name == _EVT_SCENE_QUEUE_COMPLETE:
            # bitbake's authoritative "scene queue drained" signal
            # (runqueue.summarise_scenequeue_errors). The setscene segment
            # stays active until this arrives - real tasks interleave with
            # setscene stragglers, so the first real task is NOT the end of
            # setscene.
            with self._lock:
                if self._scene_done_at is None:
                    self._scene_done_at = time.monotonic()
                    if self._scene_started_at is not None:
                        self._seg_durations.setdefault("setscene", int(self._scene_done_at - self._scene_started_at))
            return

        if class_name in (_EVT_SCENE_TASK_STARTED, _EVT_SCENE_TASK_COMPLETED, _EVT_SCENE_TASK_FAILED):
            self._update_scene(event, class_name)
            return

        if class_name == _EVT_TASK_STARTED:
            recipe, taskname = _task_key(event)
            baseline = task_timings.baseline_key(recipe or "", taskname)
            mean = self._task_baselines.get(baseline, (None, None))[0]
            logfile = getattr(event, "logfile", None)
            if self._logfile_translator and logfile:
                logfile = self._logfile_translator(logfile)
            with self._lock:
                self._running[f"{recipe}:{taskname}"] = _RunTask(
                    pf=recipe,
                    task=taskname,
                    start=time.monotonic(),
                    estimated=mean,
                    logfile=logfile,
                )
            return

        if class_name == _METADATA_EVENT and getattr(event, "type", None) == _CACHE_BACKEND_EVENT_TYPE:
            recipe, taskname = _task_key(event)
            with self._lock:
                task = self._running.get(f"{recipe}:{taskname}")
                if task is not None:
                    task.cache_backend = getattr(event, "_localdata", None)
            return

        if class_name == _EVT_TASK_FAILED:
            recipe, taskname = _task_key(event)
            logfile = getattr(event, "logfile", None)
            if self._logfile_translator and logfile:
                logfile = self._logfile_translator(logfile)
            # Tail the host log so the alert block carries the failure's
            # root cause. OSError (missing/unreadable file) must not
            # propagate -- it would crash the tailer thread.
            tail: list[str] = []
            if self._logfile_translator and logfile:
                try:
                    with open(logfile, encoding="utf-8", errors="replace") as fh:
                        tail = [ln.rstrip("\n") for ln in deque(fh, maxlen=15)]
                except OSError:
                    pass
            # One self-contained block per failure, printed below the frozen
            # frame and bitbake's error text. The trailing blank separates
            # it from whatever follows (resumed live region or the runner's
            # exit lines). The failures append is deduped against the
            # TASK_FAIL_HEAD knotty line that usually precedes this event.
            block: list[RenderableType] = [
                Text.assemble(("✗ FAILED:", "bold red"), f" {recipe} {taskname}"),
            ]
            if logfile:
                block.append(Text(f"   log: {logfile}"))
            if tail:
                block.append(Text("\n".join(tail), style="dim"))
            block.append(Text(""))
            with self._lock:
                self._running.pop(f"{recipe}:{taskname}", None)
                if (recipe, taskname) not in self._failures:
                    self._failures.append((recipe, taskname))
                    self._task_failed_count += 1
                self._pending_alerts.append(Group(*block))
            return

        if class_name in (_EVT_TASK_SUCCEEDED, _EVT_TASK_FAILED_SILENT):
            recipe, taskname = _task_key(event)
            with self._lock:
                self._running.pop(f"{recipe}:{taskname}", None)
            return

    def _reset_for_new_build_cycle(self) -> None:
        """Clear per-cycle state when a second bitbake invocation re-parses.

        ``bakar rebuild`` chains ``cleansstate && build`` in one PTY/event-feed
        session. bitbake's server unloads after each invocation (no
        ``BB_SERVER_TIMEOUT``), so the second ``bitbake`` re-parses from scratch:
        a parse/cache-load signal arriving while already in the BUILD phase marks
        a fresh cycle. Without this the build bar keeps the prior run's
        completed/total (rendering "full") and the pipeline never returns to
        parse. The global wall clock, failure record, warn/error counts, and
        column high-water widths persist across the whole command; everything
        scoped to one parse->setscene->tasks cycle resets.
        """
        with self._lock:
            self._phase = _Phase.SETUP
            self._stage = "starting"
            self._kind = ""
            self._running.clear()
            self._setscene_covered = 0
            self._setscene_total = 0
            self._setscene_notcovered = 0
            self._parse_start = None
            self._seg_durations.clear()
            self._scene_started_at = None
            self._scene_done_at = None
            self._tasks_started_at = None
            self._finished = False
            self._failed_final = False
        # Drop the prior cycle's completed count so the bar is not "full". The
        # stale total is harmless: the build bar only renders once the phase
        # flips back to BUILD, and that transition (_update_build/_update_scene)
        # sets the new cycle's total in the same event. Rich treats total=None
        # as "leave unchanged", so there is no public way to null it here anyway.
        self._build_progress.update(self._build_task_id, completed=0, kind="")
        self._setup_progress.update(self._setup_task_id, completed=0, stage="starting")

    def _update_setup(self, event: _EventStub, stage: str) -> None:
        """Map a parse/cache progress event onto the setup percentage bar."""
        current = getattr(event, "current", None)
        total = getattr(event, "total", None)
        if not total or total <= 0:
            return
        # A parse/cache signal while already building marks a second bitbake
        # invocation (rebuild's cleansstate && build): start a fresh cycle.
        if self._phase is _Phase.BUILD:
            self._reset_for_new_build_cycle()
        pct = int(current / total * 100) if current is not None else 0
        with self._lock:
            self._stage = stage
            if stage == "parsing recipes" and self._parse_start is None:
                self._parse_start = time.monotonic()
        self._setup_progress.update(self._setup_task_id, completed=pct, stage=stage)

    def _update_build(self, event: _EventStub) -> None:
        """Map a runQueueTaskStarted event onto the build bar and setscene line.

        Leaves the build total untouched (no error) when ``stats`` is absent.
        Transitions SETUP -> BUILD on the first such event.
        """
        stats = getattr(event, "stats", None)
        if stats is None:
            return
        with self._lock:
            self._phase = _Phase.BUILD
            if self._tasks_started_at is None:
                self._tasks_started_at = time.monotonic()
            self._setscene_covered = _stat(stats, "setscene_covered") or 0
            self._setscene_total = _stat(stats, "setscene_total") or 0
            self._setscene_notcovered = _stat(stats, "setscene_notcovered") or 0
        total = _stat(stats, "total")
        completed = (_stat(stats, "completed") or 0) + (_stat(stats, "active") or 0)
        self._kind = "tasks"
        if total is not None:
            self._build_progress.update(self._build_task_id, completed=completed, total=total, kind=self._kind)
        else:
            self._build_progress.update(self._build_task_id, completed=completed, kind=self._kind)

    def _update_scene(self, event: _EventStub, class_name: str) -> None:
        """Map a sceneQueue task event onto the build bar and running set.

        Transitions SETUP -> BUILD on the first setscene task so the display
        shows setscene progress (the whole setscene phase was previously invisible
        on the event feed). The bar runs ``setscene_covered + setscene_notcovered``
        of ``setscene_total``; kind is ``"setscene"``.
        """
        taskname = getattr(event, "taskname", None) or ""
        # sceneQueueEvent carries taskfile (recipe path) instead of _package.
        taskfile = getattr(event, "taskfile", None) or ""
        pf = taskfile.rsplit("/", 1)[-1].removesuffix(".bb") if taskfile else taskname
        key = f"{pf}:{taskname}"
        stats = getattr(event, "stats", None)

        if class_name == _EVT_SCENE_TASK_STARTED:
            with self._lock:
                self._phase = _Phase.BUILD
                if self._scene_started_at is None:
                    self._scene_started_at = time.monotonic()
                self._running[key] = _RunTask(pf=pf, task=taskname, start=time.monotonic())
                if stats is not None:
                    self._setscene_covered = _stat(stats, "setscene_covered") or 0
                    self._setscene_total = _stat(stats, "setscene_total") or 0
                    self._setscene_notcovered = _stat(stats, "setscene_notcovered") or 0
            if stats is not None:
                done = (_stat(stats, "setscene_covered") or 0) + (_stat(stats, "setscene_notcovered") or 0)
                self._build_progress.update(
                    self._build_task_id,
                    completed=done,
                    total=_stat(stats, "setscene_total"),
                    kind="setscene",
                )
        else:
            with self._lock:
                self._running.pop(key, None)
                if stats is not None:
                    self._setscene_covered = _stat(stats, "setscene_covered") or 0
                    self._setscene_total = _stat(stats, "setscene_total") or 0
                    self._setscene_notcovered = _stat(stats, "setscene_notcovered") or 0
            if stats is not None:
                done = (_stat(stats, "setscene_covered") or 0) + (_stat(stats, "setscene_notcovered") or 0)
                self._build_progress.update(
                    self._build_task_id,
                    completed=done,
                    total=_stat(stats, "setscene_total"),
                    kind="setscene",
                )

    def take_pending_log(self) -> str | None:
        """Return and clear a queued info message (e.g. parse completion).

        The caller emits it through its own logger so it renders with the same
        INFO tag as the other run-log lines, rather than being printed raw.
        """
        msg, self._pending_log = self._pending_log, None
        return msg

    def take_pending_alerts(self) -> list[RenderableType]:
        """Return and clear the queued failure-alert blocks.

        Mirrors take_pending_log() but drains a list: the caller prints each
        block (✗ FAILED line, host log path, log tail) below the frozen
        frame so the failure's full context lands contiguous with bitbake's
        own error lines in the chronological scrollback.
        """
        with self._lock:
            out, self._pending_alerts = self._pending_alerts, []
        return out

    def set_dist_lines(self, lines: list[RenderableType]) -> None:
        """Replace the cluster/cache header lines shown during the build (thread-safe)."""
        with self._lock:
            self._dist_lines = list(lines)

    def set_cache_badge(self, *, active: bool, hit_pct: float | None = None, verdict: str | None = None) -> None:
        """Set the live cache/dist badge state (thread-safe).

        Fed by the cache-probe thread with the cumulative-so-far hit rate and
        (sccache only) the daemon verdict. ``verdict`` stays None for a ccache
        build, which suppresses the dist badge/token.
        """
        with self._lock:
            self._cache_badge_active = active
            self._cache_hit_pct = hit_pct or 0.0
            self._dist_verdict = verdict

    def take_fail_freeze(self) -> bool:
        """Drain the one-shot freeze request set by a task-failure head line.

        The runner checks this after process_line and, when True, stops the
        Live BEFORE printing the line - committing the collapsed frame
        (pipeline, sstate, failure count) into the scrollback above the
        failure text that is about to stream.
        """
        with self._lock:
            out, self._pending_freeze = self._pending_freeze, False
        return out

    def take_pending_restart(self) -> bool:
        """Drain the one-shot restart request (regex-fallback mode only).

        Without an event feed no TaskFailed alert will arrive to end a
        failure freeze, so the next knotty task-counter line requests the
        Live resume instead.
        """
        with self._lock:
            out, self._pending_restart = self._pending_restart, False
        return out

    def notify_restarted(self) -> None:
        """Clear the freeze state after the runner restarted the Live."""
        with self._lock:
            self._fail_frozen = False
            self._pending_restart = False

    @property
    def had_task_failures(self) -> bool:
        """True when at least one task failure was recorded this build."""
        with self._lock:
            return bool(self._failures)

    def stall_report(self) -> tuple[int, list[str]] | None:
        """Seconds since the most-recently-active running task wrote its log, plus labels.

        Considers only running tasks that carry a readable host logfile. The
        returned seconds value is ``now - max(mtime)`` across those logs -- how
        long even the *freshest* running task has been silent. When it exceeds
        the abort threshold, every running task has been silent at least that
        long (the wedged-final-link signature). Returns ``None`` when nothing is
        running or no running task has a readable logfile, so the watchdog never
        aborts on an unknowable state (setscene phase, regex-fallback feed).
        """
        with self._lock:
            running = list(self._running.values())
        freshest: float | None = None
        labels: list[str] = []
        for t in running:
            if not t.logfile:
                continue
            try:
                mtime = os.stat(t.logfile).st_mtime
            except OSError:
                continue
            labels.append(f"{t.pf}:{t.task}")
            if freshest is None or mtime > freshest:
                freshest = mtime
        if freshest is None:
            return None
        return (int(time.time() - freshest), labels)

    def finish(self) -> None:
        """Mark the pipeline complete for the final rendered frame.

        Called by the build runner on a successful exit, before the Live
        region closes. Checks every reached segment and closes the last
        stage's duration clock; without this the final frame freezes with
        a spinner on whatever stage was current when kas exited (a fully
        cached build otherwise ends on "⠙ setscene" forever).
        """
        now = time.monotonic()
        with self._lock:
            self._finished = True
            if self._tasks_started_at is not None:
                self._seg_durations.setdefault("tasks", int(now - self._tasks_started_at))
            if self._scene_started_at is not None:
                # No-op when sceneQueueComplete already closed it; covers the
                # degraded regex path, which never sees that event.
                self._seg_durations.setdefault("setscene", int(now - self._scene_started_at))

    def finish_failed(self) -> None:
        """Collapse the final frame to the pipeline header and sstate line.

        Called by the build runner on a nonzero exit, before the Live
        region closes. The bar and failure list would duplicate what the
        immediate alerts and the end-of-build failure_report() carry, so
        the frozen frame keeps only the closing status.
        """
        with self._lock:
            self._failed_final = True

    def _render_breadcrumb(self) -> Text:
        """Render the pipeline header: phase segments, then the global timer.

        ``✓ parse (51s) ── ⠹ setscene   󰦗 12m34s`` (setscene running)
        ``✓ parse (51s) ── ⠹ setscene ── ⠙ tasks   󰦗 12m34s`` (overlap window)
        ``✓ parse (51s) ── ✓ setscene (2m02s) ── ⠙ tasks   󰦗 12m34s`` (scene drained)
        ``✓ parse (51s) ── ✓ setscene (2m02s)   󰦗 2m53s`` (finished, fully cached)

        Completed segments show a green check plus the stage's wall-clock
        duration, active segments the animated spinner in bold cyan, queued
        segments a hollow circle. Segment states are independent because
        bitbake's merged run queue interleaves setscene and real tasks:
        the ``tasks`` segment appears when the first real task starts while
        ``setscene`` keeps its spinner until bitbake's sceneQueueComplete
        event reports the scene queue drained - so both can spin during the
        overlap. The ``tasks`` segment never shows as queued: an sstate-warm
        build completes entirely inside setscene, and a permanently-queued
        ``○ tasks`` would advertise a stage that never runs. After
        ``finish()`` every reached segment renders checked. The timer
        follows the last segment directly, separated by its icon; it is the
        global wall clock counting from bakar start (``_start_monotonic``),
        so it spans doctor, sync, parse, and build without ever resetting.
        """
        with self._lock:
            phase = self._phase
            durations = dict(self._seg_durations)
            finished = self._finished
            scene_started = self._scene_started_at is not None
            scene_done = self._scene_done_at is not None
            tasks_started = self._tasks_started_at is not None

        spin = _SPINNER[self._frame % len(_SPINNER)]

        def done_seg(name: str) -> tuple[str, str]:
            dur = durations.get(name)
            suffix = f" ({_fmt_stall(dur)})" if dur is not None else ""
            return (f"✓ {name}{suffix}", "green")

        # The third segment is "tasks", not "build": the whole pipeline is the
        # build; this phase is the run queue executing the real (non-setscene)
        # tasks - compile, install, package, image assembly - matching the
        # bar's own "N/M tasks" vocabulary. Segment states are independent,
        # mirroring bitbake's merged run queue: setscene and real tasks
        # interleave, so during the overlap window BOTH segments carry a
        # spinner. setscene closes on sceneQueueComplete, not on the first
        # real task.
        segments: list[tuple[str, str]] = []
        if phase is _Phase.SETUP and not finished:
            segments.append((f"{spin} parse", "bold cyan"))
            segments.append(("○ setscene", "grey42"))
        else:
            segments.append(done_seg("parse"))
            if finished or scene_done or (tasks_started and not scene_started):
                # Checked when drained, at finish, or trivially complete (a
                # build whose scene queue had nothing to restore).
                segments.append(done_seg("setscene"))
            else:
                segments.append((f"{spin} setscene", "bold cyan"))
            if tasks_started:
                segments.append(done_seg("tasks") if finished else (f"{spin} tasks", "bold cyan"))

        elapsed = _fmt_stall(int(time.monotonic() - self._start_monotonic))
        text = Text()
        for i, (label, style) in enumerate(segments):
            if i:
                text.append("  ──  ", "grey30")
            text.append(label, style)
        text.append(f"   {_ICON_TIMER} {elapsed}", "bold")
        return text

    def make_renderable(self) -> Group:
        """Build the renderable for the current frame.

        Called by ``Live`` on every refresh tick -- must be fast and never block.
        """
        self._frame += 1

        now = time.monotonic()
        with self._lock:
            phase = self._phase
            setscene_covered = self._setscene_covered
            setscene_total = self._setscene_total
            setscene_notcovered = self._setscene_notcovered
            failures = list(self._failures)
            failed_final = self._failed_final
            fail_frozen = self._fail_frozen
            dist_lines = list(self._dist_lines)
            cache_badge_active = self._cache_badge_active
            cache_hit_pct = self._cache_hit_pct
            dist_verdict = self._dist_verdict
            tasks = sorted(self._running.values(), key=lambda t: -(now - t.start))

        sstate_line: Text | None = None
        if setscene_total > 0:
            pct = int(setscene_covered / setscene_total * 100)
            sstate_line = Text(
                f" {pct}% sstate ({setscene_covered} cached, {setscene_notcovered} will build)",
                style="green",
            )

        # Failure-freeze frame: committed into the scrollback when the Live
        # stops on a task-failure head line, right above the failure text.
        # Pipeline header, sstate line, then the failure count, framed by
        # blanks so the error block below reads as its own section.
        parts: list[RenderableType]
        if fail_frozen and failures:
            parts = [self._render_breadcrumb()]
            if sstate_line is not None:
                parts.append(sstate_line)
            n = len(failures)
            shown = ", ".join(f"{r}:{t}" for r, t in failures[:3])
            suffix = f" (+{n - 3} more)" if n > 3 else ""
            parts.append(Text(""))
            parts.append(Text(f" ✗ {n} failed: {shown}{suffix}", style="bold red"))
            parts.append(Text(""))
            return Group(*parts)

        # Failed-build final frame: pipeline header and sstate line only.
        # Each failure's context already printed inline under its frozen
        # frame, so the closing frame repeats none of it.
        if failed_final:
            parts = [self._render_breadcrumb()]
            if sstate_line is not None:
                parts.append(sstate_line)
            return Group(*parts)

        if phase is _Phase.SETUP:
            return Group(self._render_breadcrumb(), self._setup_progress)

        parts = [self._render_breadcrumb()]

        # Setscene-reuse line, between the pipeline header and the build bar.
        # Gated on setscene_total > 0 so the zero case leaves parts unchanged.
        if sstate_line is not None:
            parts.append(sstate_line)

        # Cluster/cache header lines, fed by the build's cache-probe thread.
        # Empty when no cache launcher is active, so the list is a no-op then.
        parts.extend(dist_lines)

        # At-a-glance cache badge (plus a dist badge for an sccache daemon).
        # Suppressed entirely when no cache backend is active.
        if cache_badge_active:
            badge = cache_render.cache_badge_rich(cache_hit_pct)
            if dist_verdict is not None:
                badge.append("  ")
                badge.append_text(cache_render.dist_badge_rich(dist_verdict))
            parts.append(badge)

        parts.append(self._build_progress)

        # Persistent failure summary during -k builds. Gated on a non-empty
        # failure list so the no-failure case leaves the render unchanged.
        if failures:
            n = len(failures)
            shown = ", ".join(f"{r}:{t}" for r, t in failures[:3])
            suffix = f" (+{n - 3} more)" if n > 3 else ""
            parts.append(Text(f" ✗ {n} failed: {shown}{suffix}", style="bold red"))

        if tasks:
            els = sorted(now - t.start for t in tasks)
            median = els[len(els) // 2]
            # Cap the table so a high-core build (32+ parallel tasks) cannot
            # grow the Live region past the terminal height, which makes Rich
            # redraw glitchy. Tasks are sorted longest-elapsed first, so the
            # rows that matter (slow / possibly stuck) always stay visible.
            overflow = len(tasks) - _MAX_TASK_ROWS
            visible = tasks[:_MAX_TASK_ROWS] if overflow > 0 else tasks
            # Build the row cells first so column widths can be derived from
            # them. The historical estimate is deliberately NOT rendered per
            # row - the prediction is too noisy to be useful as a number; it
            # feeds the stuck-task coloring instead.
            rows: list[tuple[Text, Text, Text, Text, Text, Text]] = []
            for i, t in enumerate(visible):
                elapsed = now - t.start
                icon, color = _task_style(t.task)
                spin = _SPINNER[(self._frame + i) % len(_SPINNER)]
                stuck = _stuck_color(elapsed, median, len(tasks), estimated=t.estimated)
                name = t.task.removeprefix("do_").removesuffix("_setscene")
                elapsed_cell = Text(_fmt_stall(int(elapsed)), style=stuck or "dim")
                if stuck == "bold red":
                    # Red means >4x the reference; show how far past it the
                    # task has drifted, against the same reference
                    # _stuck_color used (baseline mean, else run median).
                    ref = t.estimated if (t.estimated is not None and t.estimated > 0) else median
                    if ref > 0:
                        elapsed_cell.append(f"  {_ICON_DRIFT} +{_fmt_stall(int(elapsed - ref))}", style="bold red")
                # Recipe and task share one style so the row reads as a unit:
                # the task-type color normally, the stuck highlight when the
                # task has drifted (stuck takes the whole row, not just the pf).
                row_style = stuck or color
                # Cache-backend badge: empty glyph/colour for an unclassified task
                # (cache_backend is None), so the cell renders blank rather than a
                # placeholder - "badge iff classification recorded".
                backend_glyph, backend_colour = cache_render.cache_backend_badge(t.cache_backend)
                rows.append(
                    (
                        Text(spin, style=color),
                        Text(icon, style=color),
                        Text(backend_glyph, style=backend_colour),
                        Text(t.pf, style=row_style),
                        Text(name, style=row_style),
                        elapsed_cell,
                    )
                )
            # High-water column widths: pure auto-width recomputes from the
            # visible rows each frame, so columns jump left and right as
            # tasks start and finish. Widths grow to the longest cell seen
            # this run and never shrink, keeping every column static between
            # frames (an occasional one-time widening aside) without the
            # truncation a hardcoded width caused on long recipe names.
            self._w_pf = max(self._w_pf, *(r[3].cell_len for r in rows))
            self._w_task = max(self._w_task, *(r[4].cell_len for r in rows))
            self._w_elapsed = max(self._w_elapsed, *(r[5].cell_len for r in rows))
            table = Table(box=None, show_header=False, padding=(0, 1))
            table.add_column(width=1)  # spinner
            table.add_column(width=1)  # icon
            table.add_column(width=1)  # cache-backend badge
            table.add_column(width=self._w_pf, no_wrap=True)  # pf
            table.add_column(width=self._w_task, no_wrap=True)  # task
            table.add_column(width=self._w_elapsed, no_wrap=True)  # elapsed
            for row in rows:
                table.add_row(*row)
            parts.append(table)
            if overflow > 0:
                parts.append(Text(f"   … +{overflow} more running", style="dim"))

        return Group(*parts)

    def plain_status_line(self) -> str | None:
        """Return a plain, glyph-free status line, or None when unchanged (idle dedup).

        Reads the same ``_lock``-guarded state ``make_renderable`` renders (phase, sstate
        coverage, running count, the build bar's completed/total, elapsed) and composes one
        greppable line with no ANSI, no Rich markup, and no Nerd-Font glyph. Returns ``None``
        only when the composed line is identical to the last line this method returned - the
        emission RATE is bounded by the caller thread's tick, not by an interval here.
        """
        now = time.monotonic()
        with self._lock:
            phase = self._phase
            stage = self._stage
            kind = self._kind
            setscene_covered = self._setscene_covered
            setscene_total = self._setscene_total
            running = len(self._running)
            running_tasks = list(self._running.values())
            task = self._build_progress.tasks[0] if self._build_progress.tasks else None
            completed = int(task.completed) if task is not None else 0
            # total is None until bitbake reports it (the bar is created total=None);
            # render "?" rather than the literal None so the field stays parseable.
            total = task.total if task is not None else None
            elapsed = _fmt_stall(int(now - self._start_monotonic))
            cache_badge_active = self._cache_badge_active
            cache_hit_pct = self._cache_hit_pct
            dist_verdict = self._dist_verdict

        phase_label = stage if phase is _Phase.SETUP else (kind or "tasks")
        parts = [f"bakar[build] phase={phase_label}"]
        if setscene_total > 0:
            parts.append(f"sstate={int(setscene_covered / setscene_total * 100)}%")
        total_str = str(int(total)) if total is not None else "?"
        parts.append(f"tasks={completed}/{total_str}")
        parts.append(f"running={running}")
        parts.append(f"elapsed={elapsed}")
        # Cache/dist badge tokens, appended after the existing fields so their
        # order is preserved. Emitted only when a cache backend is active; the
        # dist token appears only for an sccache daemon (verdict set).
        if cache_badge_active:
            parts.append(cache_render.cache_badge_token(cache_hit_pct))
            if dist_verdict is not None:
                parts.append(cache_render.dist_badge_token(dist_verdict))
        # Per-task cache-backend classification tokens, deduplicated across the
        # currently running tasks so a build with many parallel tasks still
        # emits one bounded field rather than growing unbounded with the
        # running count. Omitted entirely when no running task is classified.
        backend_tokens = {
            token for t in running_tasks if (token := cache_render.cache_backend_token(t.cache_backend)) is not None
        }
        if backend_tokens:
            parts.append(f"cache_backend={','.join(sorted(backend_tokens))}")
        line = " ".join(parts)

        with self._lock:
            if line == self._plain_last_line:
                return None
            self._plain_last_line = line
        return line
