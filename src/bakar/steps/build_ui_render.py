"""Rich renderer for ``BuildUIState``.

Pure functions that read a ``BuildUIState`` and produce the Live renderable, the
pipeline breadcrumb, or the plain status line, plus the helpers that construct
the Rich objects the state holds (``default_progress_factory``,
``failure_alert``). The functions hold no state of their own, so the build UI
state machine in ``build_ui`` stays independent of how it is drawn.

This module imports ``build_ui`` at module level for the rich-free ``_Phase`` and
``_fmt_stall``; ``build_ui`` therefore imports this module only inside functions.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from bakar import cache_render
from bakar.steps.build_ui import _fmt_stall, _Phase

if TYPE_CHECKING:
    from bakar.steps.build_ui import BuildUIState

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


def default_progress_factory() -> tuple[Progress, Progress]:
    """Build the (setup, build) ``Progress`` pair a ``BuildUIState`` owns.

    Plain objects, never entered as context managers: the outer ``Live`` owns the
    terminal refresh loop and a second internal ``Live`` would corrupt the cursor.
    """
    setup = Progress(
        TextColumn("[cyan]{task.fields[stage]}[/]"),
        BarColumn(style="grey30", complete_style="cyan", finished_style="green"),
        TextColumn("{task.percentage:>3.0f}%"),
    )
    build = Progress(
        TextColumn("[cyan]kas_build[/]"),
        BarColumn(style="grey30", complete_style="cyan", finished_style="green"),
        TextColumn("{task.completed}/{task.total} {task.fields[kind]}"),
    )
    return setup, build


def failure_alert(recipe: str, taskname: str, logfile: str | None, tail: list[str]) -> Group:
    """Build the one-shot alert block printed below the frozen frame for a failed task."""
    block: list[RenderableType] = [
        Text.assemble(("✗ FAILED:", "bold red"), f" {recipe} {taskname}"),
    ]
    if logfile:
        block.append(Text(f"   log: {logfile}"))
    if tail:
        block.append(Text("\n".join(tail), style="dim"))
    block.append(Text(""))
    return Group(*block)


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


def _render_breadcrumb(state: BuildUIState) -> Text:
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
    with state._lock:
        phase = state._phase
        durations = dict(state._seg_durations)
        finished = state._finished
        scene_started = state._scene_started_at is not None
        scene_done = state._scene_done_at is not None
        tasks_started = state._tasks_started_at is not None

    spin = _SPINNER[state._frame % len(_SPINNER)]

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

    elapsed = _fmt_stall(int(time.monotonic() - state._start_monotonic))
    text = Text()
    for i, (label, style) in enumerate(segments):
        if i:
            text.append("  ──  ", "grey30")
        text.append(label, style)
    text.append(f"   {_ICON_TIMER} {elapsed}", "bold")
    return text


def make_renderable(state: BuildUIState) -> Group:
    """Build the renderable for the current frame.

    Called by ``Live`` on every refresh tick -- must be fast and never block.
    """
    state._frame += 1

    now = time.monotonic()
    with state._lock:
        phase = state._phase
        setscene_covered = state._setscene_covered
        setscene_total = state._setscene_total
        setscene_notcovered = state._setscene_notcovered
        failures = list(state._failures)
        failed_final = state._failed_final
        fail_frozen = state._fail_frozen
        dist_lines = list(state._dist_lines)
        cache_badge_active = state._cache_badge_active
        cache_hit_pct = state._cache_hit_pct
        dist_verdict = state._dist_verdict
        tasks = sorted(state._running.values(), key=lambda t: -(now - t.start))

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
        parts = [_render_breadcrumb(state)]
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
        parts = [_render_breadcrumb(state)]
        if sstate_line is not None:
            parts.append(sstate_line)
        return Group(*parts)

    if phase is _Phase.SETUP:
        return Group(_render_breadcrumb(state), state._setup_progress)

    parts = [_render_breadcrumb(state)]

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

    parts.append(state._build_progress)

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
            spin = _SPINNER[(state._frame + i) % len(_SPINNER)]
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
        state._w_pf = max(state._w_pf, *(r[3].cell_len for r in rows))
        state._w_task = max(state._w_task, *(r[4].cell_len for r in rows))
        state._w_elapsed = max(state._w_elapsed, *(r[5].cell_len for r in rows))
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(width=1)  # spinner
        table.add_column(width=1)  # icon
        table.add_column(width=1)  # cache-backend badge
        table.add_column(width=state._w_pf, no_wrap=True)  # pf
        table.add_column(width=state._w_task, no_wrap=True)  # task
        table.add_column(width=state._w_elapsed, no_wrap=True)  # elapsed
        for row in rows:
            table.add_row(*row)
        parts.append(table)
        if overflow > 0:
            parts.append(Text(f"   … +{overflow} more running", style="dim"))

    return Group(*parts)


def plain_status_line(state: BuildUIState) -> str | None:
    """Return a plain, glyph-free status line, or None when unchanged (idle dedup).

    Reads the same ``_lock``-guarded state ``make_renderable`` renders (phase, sstate
    coverage, running count, the build bar's completed/total, elapsed) and composes one
    greppable line with no ANSI, no Rich markup, and no Nerd-Font glyph. Returns ``None``
    only when the composed line is identical to the last line this method returned - the
    emission RATE is bounded by the caller thread's tick, not by an interval here.
    """
    now = time.monotonic()
    with state._lock:
        phase = state._phase
        stage = state._stage
        kind = state._kind
        setscene_covered = state._setscene_covered
        setscene_total = state._setscene_total
        running = len(state._running)
        backend_tokens = {t.cache_backend for t in state._running.values() if t.cache_backend is not None}
        task = state._build_progress.tasks[0] if state._build_progress.tasks else None
        completed = int(task.completed) if task is not None else 0
        # total is None until bitbake reports it (the bar is created total=None);
        # render "?" rather than the literal None so the field stays parseable.
        total = task.total if task is not None else None
        elapsed = _fmt_stall(int(now - state._start_monotonic))
        cache_badge_active = state._cache_badge_active
        cache_hit_pct = state._cache_hit_pct
        dist_verdict = state._dist_verdict

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
    if backend_tokens:
        parts.append(f"cache_backend={','.join(sorted(backend_tokens))}")
    line = " ".join(parts)

    with state._lock:
        if line == state._plain_last_line:
            return None
        state._plain_last_line = line
    return line
