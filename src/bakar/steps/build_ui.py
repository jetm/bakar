"""Rich Live UI state for the kas-container build progress display.

Parses bitbake knotty's **non-interactive fallback** output lines and renders a
phase-aware Rich Live display:

- SETUP phase (loading cache / parsing recipes) renders a single percentage bar.
- BUILD phase (setscene reuse + task execution) renders an X-of-Y counter bar
  plus a live per-task table sorted by elapsed descending.

The transition to BUILD happens on the first ``Running [setscene] task N of M``
line. The running-task set is reconstructed from ``recipe PF: task T: Started``
and ``: Succeeded``/``: Failed`` lifecycle events, keyed on ``PF:task``, with
elapsed computed from the local monotonic clock.

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

from bakar.fmt import fmt_bytes

# Knotty fallback line formats (non-interactive mode inside the kas container).
LOADING_CACHE = re.compile(r"Loading cache:\s+(\d+)%")
PARSE_PROGRESS = re.compile(r"Parsing recipes:\s+(\d+)%")
RUNNING_TASK = re.compile(r"Running (setscene )?task (\d+) of (\d+)")  # g1=setscene?, g2=N, g3=M
RECIPE_STARTED = re.compile(r"recipe (\S+): task (do_\S+): Started")
RECIPE_DONE = re.compile(r"recipe (\S+): task (do_\S+): (?:Succeeded|Failed)")
FALLBACK_MODE = re.compile(r"Unable to use interactive mode")

# Lines to surface above the Live display so users see real problems.
SEVERITY_PASSTHROUGH = re.compile(r"\b(ERROR|FATAL|WARNING|QA Issue):")


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


def _fmt_du(delta: int) -> str:
    if delta <= 0:
        return "-"
    return "+" + fmt_bytes(delta)


class BuildUIState:
    """Mutable UI state that parses knotty fallback lines and renders a Rich Live display.

    Construct the two ``Progress`` objects as plain objects — do NOT use them as
    context managers. The outer ``Live(get_renderable=ui.make_renderable)`` owns
    the terminal refresh loop; entering ``Progress`` as a context manager would
    start a second internal ``Live`` and corrupt the cursor.

    Thread safety: ``_running`` mutations are guarded by ``_lock``. ``process_line``
    and ``update_heartbeat`` may be called from the pump/heartbeat threads;
    ``make_renderable`` is called from the ``Live`` refresh thread.
    """

    def __init__(self) -> None:
        self._setup_progress = Progress(
            TextColumn("[cyan]{task.fields[stage]}[/]"),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
        )
        self._setup_task_id = self._setup_progress.add_task("setup", total=100, stage="setup")

        self._build_progress = Progress(
            TextColumn("[cyan]kas_build[/]"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} {task.fields[kind]}"),
            TextColumn("[dim]{task.fields[stall]}  {task.fields[du]}[/]"),
            TimeElapsedColumn(),
        )
        self._build_task_id = self._build_progress.add_task("build", total=None, kind="", stall="0s", du="-")

        self._phase = _Phase.SETUP
        self._running: dict[str, _RunTask] = {}
        self._lock = threading.Lock()
        self.fallback_detected = False

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
            self._setup_progress.update(self._setup_task_id, completed=int(m.group(1)), stage="parsing")
            return None

        # 3. Cache load progress.
        m = LOADING_CACHE.search(line)
        if m:
            self._setup_progress.update(self._setup_task_id, completed=int(m.group(1)), stage="loading cache")
            return None

        # 4. Task counter — flips to the BUILD phase.
        m = RUNNING_TASK.search(line)
        if m:
            self._phase = _Phase.BUILD
            kind = "setscene" if m.group(1) else "tasks"
            self._build_progress.update(
                self._build_task_id,
                completed=int(m.group(2)),
                total=int(m.group(3)),
                kind=kind,
            )
            return None

        # 5. Task started — add to the running set.
        m = RECIPE_STARTED.search(line)
        if m:
            key = f"{m.group(1)}:{m.group(2)}"
            with self._lock:
                self._running[key] = _RunTask(pf=m.group(1), task=m.group(2), start=time.monotonic())
            return None

        # 6. Task done — remove from the running set.
        m = RECIPE_DONE.search(line)
        if m:
            key = f"{m.group(1)}:{m.group(2)}"
            with self._lock:
                self._running.pop(key, None)
            return None

        # 7. Severity lines surface above the Live display.
        if SEVERITY_PASSTHROUGH.search(line):
            return line

        # 8. Default.
        return None

    def update_heartbeat(self, stall_secs: int, du_delta: int) -> None:
        """Update stall clock and disk-usage delta fields on the build bar."""
        self._build_progress.update(
            self._build_task_id,
            stall=_fmt_stall(stall_secs),
            du=_fmt_du(du_delta),
        )

    def make_renderable(self) -> Group:
        """Build a ``Group`` for the current frame.

        Called by ``Live`` on every refresh tick — must be fast and never block.
        """
        if self._phase is _Phase.SETUP:
            return Group(self._setup_progress)

        parts: list[RenderableType] = [self._build_progress]

        now = time.monotonic()
        with self._lock:
            tasks = sorted(self._running.values(), key=lambda t: -(now - t.start))

        if tasks:
            table = Table(box=None, show_header=False, padding=(0, 1))
            table.add_column(no_wrap=True, max_width=36)
            table.add_column(style="cyan", width=28)
            table.add_column(style="dim", justify="right", width=8)
            for t in tasks:
                table.add_row(
                    t.pf,
                    t.task.removeprefix("do_"),
                    _fmt_stall(int(now - t.start)),
                )
            parts.append(table)

        return Group(*parts)
