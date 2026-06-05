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

from bakar import task_timings
from bakar.eventlog import (
    _RUNQUEUE_TASK_STARTED,
    _TASK_FAILED,
    _TASK_FAILED_SILENT,
    _TASK_STARTED,
    _TASK_SUCCEEDED,
    _stat,
    _task_key,
)

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
_ICON_SETSCENE = ""  # refresh
_ICON_TIMER = "󰦗"  # md-progress-clock (build elapsed)
_ICON_DRIFT = ""  # fa-warning (stuck task: time drifted past its reference)


class _Phase(Enum):
    SETUP = "setup"
    BUILD = "build"


@dataclass(slots=True)
class _RunTask:
    pf: str
    task: str
    start: float  # time.monotonic() at Started
    estimated: float | None = None  # historical mean seconds for this taskname, if known


def _fmt_stall(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


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
    if any(k in task for k in ("package", "deploy", "image", "rootfs", "spdx", "install")):
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
    and ``update_heartbeat`` may be called from the pump/heartbeat threads;
    ``make_renderable`` is called from the ``Live`` refresh thread.
    """

    def __init__(
        self,
        start_monotonic: float | None = None,
        logfile_translator: Callable[[str], str] | None = None,
        timings_path: Path | None = None,
    ) -> None:
        """``start_monotonic`` is the ``time.monotonic()`` stamp of when ``bakar``
        started (RunLogger captures it before doctor). When given, the global
        timer on the pipeline header counts from there -- including doctor,
        sync, and parse -- instead of from this object's construction.

        ``timings_path`` selects the context-scoped baseline file (see
        ``task_timings.timings_path_for``); ``None`` falls back to the legacy
        global file.
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
        self._build_sub_phase: str = "setscene"
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
        # Persistent record of (recipe, taskname) for each failed task, rendered
        # in the build bar. ``_pending_alerts`` queues one-shot alert lines the
        # caller drains via take_pending_alerts() and prints above the Live.
        self._failures: list[tuple[str, str]] = []
        self._pending_alerts: list[str] = []
        self._task_failed_count: int = 0
        # Tail of the most recent failed task's log file, rendered under the
        # failure-list line. Replaced (not appended) on each failure.
        self._failure_preview: list[str] = []
        # Historical timing baselines {"recipe:task": (mean, stddev)} from
        # prior builds of THIS context (workspace+machine+mode). Empty on the
        # first build -> estimated stays None and stuck-task detection falls
        # back to the current-run median.
        self._task_baselines = task_timings.load_baselines(timings_path)

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
            # 2. Parse phase progress.
            m = PARSE_PROGRESS.search(line)
            if m:
                self._stage = "parsing recipes"
                if self._parse_start is None:
                    self._parse_start = time.monotonic()
                self._setup_progress.update(self._setup_task_id, completed=int(m.group(1)), stage=self._stage)
                return None

            # 3. Cache load progress.
            m = LOADING_CACHE.search(line)
            if m:
                self._stage = "loading cache"
                self._setup_progress.update(self._setup_task_id, completed=int(m.group(1)), stage=self._stage)
                return None

            # 4. Task counter -- flips to the BUILD phase.
            m = RUNNING_TASK.search(line)
            if m:
                with self._lock:
                    entering_build = self._phase is _Phase.SETUP
                    self._phase = _Phase.BUILD
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
                # lines), reporting how long parsing took.
                if entering_build:
                    took = ""
                    if self._parse_start is not None:
                        took = f" ({_fmt_stall(int(time.monotonic() - self._parse_start))})"
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
                    took = f" ({_fmt_stall(int(time.monotonic() - self._parse_start))})"
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

        if class_name in (_EVT_SCENE_TASK_STARTED, _EVT_SCENE_TASK_COMPLETED, _EVT_SCENE_TASK_FAILED):
            self._update_scene(event, class_name)
            return

        if class_name == _EVT_TASK_STARTED:
            recipe, taskname = _task_key(event)
            baseline = task_timings.baseline_key(recipe or "", taskname)
            mean = self._task_baselines.get(baseline, (None, None))[0]
            with self._lock:
                self._running[f"{recipe}:{taskname}"] = _RunTask(
                    pf=recipe,
                    task=taskname,
                    start=time.monotonic(),
                    estimated=mean,
                )
            return

        if class_name == _EVT_TASK_FAILED:
            recipe, taskname = _task_key(event)
            logfile = getattr(event, "logfile", None)
            if self._logfile_translator and logfile:
                logfile = self._logfile_translator(logfile)
            msg = f"[bold red]✗ FAILED:[/] {recipe} {taskname}"
            if logfile:
                msg += f"\n   log: {logfile}"
            with self._lock:
                self._running.pop(f"{recipe}:{taskname}", None)
                self._failures.append((recipe, taskname))
                self._pending_alerts.append(msg)
                self._task_failed_count += 1
            # Tail the host log so the most recent failure's last lines render
            # under the failure summary. OSError (missing/unreadable file) must
            # not propagate -- it would crash the tailer thread.
            if self._logfile_translator and logfile:
                try:
                    with open(logfile, encoding="utf-8", errors="replace") as fh:
                        lines = list(deque(fh, maxlen=15))
                except OSError:
                    pass
                else:
                    with self._lock:
                        self._failure_preview = [ln.rstrip("\n") for ln in lines]
            return

        if class_name in (_EVT_TASK_SUCCEEDED, _EVT_TASK_FAILED_SILENT):
            recipe, taskname = _task_key(event)
            with self._lock:
                self._running.pop(f"{recipe}:{taskname}", None)
            return

    def _update_setup(self, event: _EventStub, stage: str) -> None:
        """Map a parse/cache progress event onto the setup percentage bar."""
        current = getattr(event, "current", None)
        total = getattr(event, "total", None)
        if not total or total <= 0:
            return
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
            self._build_sub_phase = "real_tasks"
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
                self._build_sub_phase = "setscene"
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

    def take_pending_alerts(self) -> list[str]:
        """Return and clear the queued failure-alert lines.

        Mirrors take_pending_log() but drains a list: the caller prints each
        alert above the Live display so failures stay visible during a ``-k``
        build.
        """
        with self._lock:
            out, self._pending_alerts = self._pending_alerts, []
        return out

    def update_heartbeat(self, stall_secs: int, du_delta: int) -> None:
        """Retained for caller compatibility.

        The build line no longer shows a stall clock or a disk-usage delta -- the
        global timer is the only liveness readout -- so this is a no-op.
        """

    def _render_breadcrumb(self) -> Text:
        """Render the pipeline header: phase segments, then the global timer.

        ``✓ parse ── ⠹ setscene ── ○ tasks   󰦗 12m34s``

        Completed segments show a green check, the active segment carries the
        animated spinner in bold cyan, queued segments a hollow circle. The
        timer follows the build segment directly, separated by its icon; it
        is the global wall clock counting from bakar start
        (``_start_monotonic``), so it spans doctor, sync, parse, and build
        without ever resetting.
        """
        with self._lock:
            phase = self._phase
            sub_phase = self._build_sub_phase

        spin = _SPINNER[self._frame % len(_SPINNER)]
        current = ("bold cyan", f"{spin} ")
        done = ("green", "✓ ")
        future = ("grey42", "○ ")

        if phase is _Phase.SETUP:
            parse, setscene, build = current, future, future
        elif sub_phase == "setscene":
            parse, setscene, build = done, current, future
        else:  # BUILD, real_tasks
            parse, setscene, build = done, done, current

        elapsed = _fmt_stall(int(time.monotonic() - self._start_monotonic))
        # The third segment is "tasks", not "build": the whole pipeline is the
        # build; this phase is the run queue executing the real (non-setscene)
        # tasks - compile, install, package, image assembly - matching the
        # bar's own "N/M tasks" vocabulary.
        return Text.assemble(
            (f"{parse[1]}parse", parse[0]),
            ("  ──  ", "grey30"),
            (f"{setscene[1]}setscene", setscene[0]),
            ("  ──  ", "grey30"),
            (f"{build[1]}tasks", build[0]),
            (f"   {_ICON_TIMER} {elapsed}", "dim"),
        )

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
            failure_preview = list(self._failure_preview)
            tasks = sorted(self._running.values(), key=lambda t: -(now - t.start))

        if phase is _Phase.SETUP:
            return Group(self._render_breadcrumb(), self._setup_progress)

        parts: list[RenderableType] = [self._render_breadcrumb(), self._build_progress]

        # Setscene-reuse line, between the build bar and the per-task table.
        # Gated on setscene_total > 0 so the zero case leaves parts unchanged.
        if setscene_total > 0:
            pct = int(setscene_covered / setscene_total * 100) if setscene_total > 0 else 0
            parts.append(
                Text(
                    f" {pct}% sstate ({setscene_covered} cached, {setscene_notcovered} will build)",
                    style="green",
                )
            )

        # Persistent failure summary during -k builds. Gated on a non-empty
        # failure list so the no-failure case leaves the render unchanged.
        if failures:
            n = len(failures)
            shown = ", ".join(f"{r}:{t}" for r, t in failures[:3])
            suffix = f" (+{n - 3} more)" if n > 3 else ""
            parts.append(Text(f" ✗ {n} failed: {shown}{suffix}", style="bold red"))
            # Tail of the most recent failure's log, below the summary line.
            if failure_preview:
                parts.append(Text("\n".join(failure_preview), style="dim"))

        if tasks:
            els = sorted(now - t.start for t in tasks)
            median = els[len(els) // 2]
            # Cap the table so a high-core build (32+ parallel tasks) cannot
            # grow the Live region past the terminal height, which makes Rich
            # redraw glitchy. Tasks are sorted longest-elapsed first, so the
            # rows that matter (slow / possibly stuck) always stay visible.
            overflow = len(tasks) - _MAX_TASK_ROWS
            visible = tasks[:_MAX_TASK_ROWS] if overflow > 0 else tasks
            # Auto-width columns: Rich sizes each to its longest visible cell,
            # so elapsed hugs the task name instead of sitting across a wide
            # fixed column. The historical estimate is deliberately NOT
            # rendered per row - the prediction is too noisy to be useful as
            # a number; it feeds the stuck-task coloring instead.
            table = Table(box=None, show_header=False, padding=(0, 1))
            table.add_column(width=1)  # spinner
            table.add_column(width=1)  # icon
            table.add_column(no_wrap=True, max_width=34)  # pf
            table.add_column()  # task
            table.add_column()  # elapsed
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
                table.add_row(
                    Text(spin, style=color),
                    Text(icon, style=color),
                    Text(t.pf, style=stuck or "default"),
                    Text(name, style=color),
                    elapsed_cell,
                )
            parts.append(table)
            if overflow > 0:
                parts.append(Text(f"   … +{overflow} more running", style="dim"))

        return Group(*parts)
