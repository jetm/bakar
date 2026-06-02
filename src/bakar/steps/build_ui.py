"""Rich Live UI state for the kas-container build progress display.

Parses bitbake knotty PTY output lines and renders a multi-component
Rich Live display: main progress bar, optional setscene bar, per-task
table sorted by elapsed descending.

``BuildUIState`` is designed to be console-agnostic: ``process_line``
returns a passthrough string for severity lines and ``None`` otherwise so
the caller can forward severity messages to ``live.console.print()``
without coupling this class to any console or Live instance.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from rich.console import Group
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from bakar.fmt import fmt_bytes

# Bitbake knotty footer: dominant signal during execution phase.
# Format: "Currently  N running tasks (X of Y)  PP% |###|"
CURRENT_RUNNING = re.compile(r"Currently\s+(\d+) running tasks \((\d+) of (\d+)\)")

# Setscene (sstate reuse) progress footer line.
# Format: "Setscene tasks: X of Y"
SETSCENE_RUNNING = re.compile(r"Setscene tasks: (\d+) of (\d+)")

# Per-task footer lines emitted by knotty for each running task.
# Format: "N: PF do_task - elapsed (pid P)"  or  "N: PF do_task (pid P)"
KNOTTY_TASK_RE = re.compile(r"^(\d+): (\S+) (do_\w+)(?:\s+-\s+(\S+))?\s+\(pid (\d+)\)")

# Fallback for non-TTY mode (filtered by InteractConsoleLogFilter in TTY mode).
# Format: "NOTE: Running task N of Y (recipe:do_task)"
RUNNING_TASK = re.compile(r"NOTE: Running task (\d+) of (\d+) \(([^)]+)\)")

# Parse phase progress (before execution starts).
# Format: "Parsing recipes: PP% |...| N/Y"
PARSE_PROGRESS = re.compile(r"Parsing recipes: (\d+)% \|[^|]*\| (\d+)/(\d+)")

# Lines to surface above the Live display so users see real problems.
SEVERITY_PASSTHROUGH = re.compile(r"\b(ERROR|FATAL|WARNING|QA Issue):")


@dataclass(slots=True)
class _RunTask:
    slot: int
    pf: str
    task: str
    elapsed: str


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


def _elapsed_secs(s: str) -> int:
    """Parse knotty elapsed strings like ``"1h2m5s"``, ``"2m15s"``, ``"47s"`` to seconds."""
    if not s:
        return 0
    total = 0
    if h := re.search(r"(\d+)h", s):
        total += int(h.group(1)) * 3600
    if m := re.search(r"(\d+)m", s):
        total += int(m.group(1)) * 60
    if sec := re.search(r"(\d+)s", s):
        total += int(sec.group(1))
    return total


class BuildUIState:
    """Mutable UI state that parses knotty lines and renders a Rich Live display.

    Construct as a plain object — do NOT use ``progress`` or ``_setscene`` as
    context managers. The outer ``Live(get_renderable=ui.make_renderable)`` owns
    the terminal refresh loop; entering ``Progress`` as a context manager would
    start a second internal ``Live`` and corrupt the cursor.

    Thread safety: ``_running`` mutations are guarded by ``_lock``. ``process_line``
    and ``update_heartbeat`` may be called from the pump/heartbeat threads;
    ``make_renderable`` is called from the ``Live`` refresh thread.
    """

    def __init__(self) -> None:
        self.progress = Progress(
            TextColumn("[cyan]kas_build[/]"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("[dim]{task.fields[stall]}  {task.fields[du]}[/]"),
            TimeElapsedColumn(),
        )
        self._task_id = self.progress.add_task("build", total=None, stall="0s", du="-")

        self._setscene = Progress(
            TextColumn("[dim]setscene[/]"),
            BarColumn(bar_width=20),
            TextColumn("[dim]{task.completed}/{task.total}[/]"),
        )
        self._setscene_task_id = self._setscene.add_task("setscene", total=0)

        self._running: dict[int, _RunTask] = {}
        self._new_frame: bool = False
        self._lock = threading.Lock()
        self._setscene_total = 0
        self._last_total = 0
        self._expansion_count = 0

    def process_line(self, line: str) -> str | None:
        """Parse one line of knotty PTY output and update internal state.

        Returns a Rich markup string for severity lines so the caller can
        forward it to ``live.console.print()``. Returns ``None`` for all
        other lines.
        """
        # 1. Main execution progress
        m = CURRENT_RUNNING.search(line)
        if m:
            completed = int(m.group(2))
            total = int(m.group(3))
            self.progress.update(self._task_id, completed=completed, total=total)
            msg = None
            if self._last_total > 0 and abs(total - self._last_total) / max(self._last_total, 1) >= 0.05:
                self._expansion_count += 1
                verb = "expanded" if total > self._last_total else "reduced"
                msg = f"[yellow]task graph {verb}: {self._last_total} -> {total}[/]"
            self._new_frame = True
            self._last_total = total
            return msg

        # 2. Setscene progress
        m = SETSCENE_RUNNING.search(line)
        if m:
            self._setscene_total = int(m.group(2))
            self._setscene.update(
                self._setscene_task_id,
                completed=int(m.group(1)),
                total=self._setscene_total,
            )
            return None

        # 3. Per-task slot line
        m = KNOTTY_TASK_RE.match(line)
        if m:
            slot = int(m.group(1))
            pf = m.group(2)
            task = m.group(3)
            elapsed = m.group(4) or ""
            pid = int(m.group(5))
            with self._lock:
                if self._new_frame:
                    self._running.clear()
                    self._new_frame = False
                self._running[pid] = _RunTask(slot=slot, pf=pf, task=task, elapsed=elapsed)
            return None

        # 4. Parse phase progress
        m = PARSE_PROGRESS.search(line)
        if m:
            self.progress.update(self._task_id, completed=int(m.group(2)), total=int(m.group(3)))
            return None

        # 5. Severity lines surface above the Live display
        if SEVERITY_PASSTHROUGH.search(line):
            return line

        # 6. TTY-filtered fallback: non-TTY task start lines (dead in interactive mode)
        m = RUNNING_TASK.search(line)
        if m:
            self.progress.update(self._task_id, completed=int(m.group(1)), total=int(m.group(2)))
            return None

        return None

    def update_heartbeat(self, stall_secs: int, du_delta: int) -> None:
        """Update stall clock and disk-usage delta fields on the main bar."""
        self.progress.update(
            self._task_id,
            stall=_fmt_stall(stall_secs),
            du=_fmt_du(du_delta),
        )

    def make_renderable(self) -> Group:
        """Build a ``Group`` for the current frame.

        Called by ``Live`` on every refresh tick — must be fast and never block.
        """
        parts: list = [self.progress]

        if self._setscene_total > 0:
            parts.append(self._setscene)

        with self._lock:
            tasks = sorted(self._running.values(), key=lambda t: -_elapsed_secs(t.elapsed))

        if tasks:
            table = Table(box=None, show_header=False, padding=(0, 1))
            table.add_column(style="dim", width=3, justify="right")
            table.add_column(no_wrap=True, max_width=32)
            table.add_column(style="cyan", width=22)
            table.add_column(style="dim", justify="right", width=8)
            for t in tasks:
                table.add_row(
                    str(t.slot),
                    t.pf,
                    t.task.removeprefix("do_"),
                    t.elapsed,
                )
            parts.append(table)

        return Group(*parts)
