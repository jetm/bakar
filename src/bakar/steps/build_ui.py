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
from dataclasses import dataclass
from enum import Enum

from rich.console import Group, RenderableType
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

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

# Nerd-Font icons (Font Awesome private-use range, confirmed present in
# IosevkaTerm / CommitMono Nerd Font). Truecolor + Nerd Font assumed.
_ICON_COMPILE = ""  # cog
_ICON_FETCH = ""  # download
_ICON_CONFIGURE = ""  # wrench
_ICON_PACKAGE = ""  # package
_ICON_SETSCENE = ""  # refresh
_ICON_TIMER = "󰦗"  # md-progress-clock (build elapsed)


class _Phase(Enum):
    SETUP = "setup"
    BUILD = "build"


@dataclass(slots=True)
class _RunTask:
    pf: str
    task: str
    start: float  # time.monotonic() at Started


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


def _stuck_color(elapsed: float, median: float, count: int) -> str | None:
    """Return a highlight color when a task runs far longer than the median, else None."""
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

    def __init__(self, start_monotonic: float | None = None) -> None:
        """``start_monotonic`` is the ``time.monotonic()`` stamp of when ``bakar``
        started (RunLogger captures it before doctor). When given, the global
        timer counts from there -- including doctor, sync, and parse -- instead
        of from this object's construction.
        """
        self._setup_progress = Progress(
            TextColumn("[cyan]{task.fields[stage]}[/]"),
            BarColumn(style="grey30", complete_style="cyan", finished_style="green"),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
        )
        self._setup_task_id = self._setup_progress.add_task("setup", total=100, stage="starting")

        # The build task's elapsed column is the global wall-clock timer: it
        # includes parse and is never reset across the parse->build transition.
        # The timer icon prefixes the elapsed column.
        self._build_progress = Progress(
            TextColumn("[cyan]kas_build[/]"),
            BarColumn(style="grey30", complete_style="cyan", finished_style="green"),
            TextColumn("{task.completed}/{task.total} {task.fields[kind]}"),
            TextColumn(f"[dim]{_ICON_TIMER}[/]"),
            TimeElapsedColumn(),
        )
        self._build_task_id = self._build_progress.add_task("build", total=None, kind="")
        if start_monotonic is not None:
            # Backdate the timer to the start of the bakar run.
            self._build_progress.tasks[0].start_time = start_monotonic

        self._phase = _Phase.SETUP
        self._stage = "starting"
        self._kind = ""
        self._running: dict[str, _RunTask] = {}
        self._lock = threading.Lock()
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

    def process_line(self, line: str) -> str | None:
        """Parse one line of knotty fallback output and update internal state.

        Returns a Rich markup string for severity lines so the caller can
        forward it to ``live.console.print()``. Returns ``None`` for all
        other lines.
        """
        # 1. Mode detection: record that the fallback parser path is active.
        if FALLBACK_MODE.search(line):
            self.fallback_detected = True
            return None

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

    def take_pending_log(self) -> str | None:
        """Return and clear a queued info message (e.g. parse completion).

        The caller emits it through its own logger so it renders with the same
        INFO tag as the other run-log lines, rather than being printed raw.
        """
        msg, self._pending_log = self._pending_log, None
        return msg

    def update_heartbeat(self, stall_secs: int, du_delta: int) -> None:
        """Retained for caller compatibility.

        The build line no longer shows a stall clock or a disk-usage delta -- the
        global timer is the only liveness readout -- so this is a no-op.
        """

    def make_renderable(self) -> Group:
        """Build the renderable for the current frame.

        Called by ``Live`` on every refresh tick -- must be fast and never block.
        """
        self._frame += 1

        if self._phase is _Phase.SETUP:
            return Group(self._setup_progress)

        parts: list[RenderableType] = [self._build_progress]

        now = time.monotonic()
        with self._lock:
            tasks = sorted(self._running.values(), key=lambda t: -(now - t.start))

        if tasks:
            els = sorted(now - t.start for t in tasks)
            median = els[len(els) // 2]
            table = Table(box=None, show_header=False, padding=(0, 1))
            table.add_column(width=1)  # spinner
            table.add_column(width=1)  # icon
            table.add_column(no_wrap=True, max_width=34)  # pf
            table.add_column(width=24)  # task
            table.add_column(justify="right", width=8)  # elapsed
            for i, t in enumerate(tasks):
                elapsed = now - t.start
                icon, color = _task_style(t.task)
                spin = _SPINNER[(self._frame + i) % len(_SPINNER)]
                stuck = _stuck_color(elapsed, median, len(tasks))
                name = t.task.removeprefix("do_").removesuffix("_setscene")
                table.add_row(
                    Text(spin, style=color),
                    Text(icon, style=color),
                    Text(t.pf, style=stuck or "default"),
                    Text(name, style=color),
                    Text(_fmt_stall(int(elapsed)), style=stuck or "dim"),
                )
            parts.append(table)

        return Group(*parts)
