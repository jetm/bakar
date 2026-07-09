"""Per-task timing and top-N-slowest report.

bitbake records per-task ``started``/``completed`` timestamps in the
normalized ``bitbake-events.json`` artifact (see :mod:`bakar.eventlog`).
:func:`timing_report` turns those rows into a ranked "where did my build time
go" view: the top-N slowest individual tasks, each annotated with the prior
cross-build mean/stddev already tracked by :mod:`bakar.task_timings` (no
second baseline store is built here).

The tasks-list extraction and missing/negative-duration guard reuse
:func:`bakar.task_rollup._tasks_from` rather than re-parsing the ``tasks``
list a third time (see design.md's "reuse ``_tasks_from``" decision).

This module intentionally exposes only the pure, no-live-build part of the
timing report (durations + top-N + baseline context). A later task adds a
critical-path sub-section computed from ``bakar graph``'s live dependency
model; :class:`CriticalPath` is the seam that section plugs into - it always
reports ``available=False`` here so callers can render "critical-path
unavailable" without knowing whether that section has been wired in yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar import task_timings
from bakar.task_rollup import _tasks_from

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_TOP_N = 10


@dataclass(frozen=True)
class TaskDuration:
    """One task's wall-clock duration with optional baseline context.

    ``baseline_mean``/``baseline_stddev`` are ``None`` when no prior baseline
    exists for this task's ``"<recipe-sans-version>:<task>"`` key.
    """

    recipe: str
    task: str
    duration: float
    baseline_mean: float | None = None
    baseline_stddev: float | None = None


@dataclass(frozen=True)
class CriticalPath:
    """Seam for the critical-path sub-section (filled in by a later task).

    ``available`` stays ``False`` until the critical-path step (which
    requires a live ``bakar graph`` dependency-model lookup) is wired in or
    when that lookup fails/is skipped; the duration and top-N sections above
    never depend on this section's state.
    """

    available: bool = False
    chain: list[str] = field(default_factory=list)
    total_seconds: float = 0.0
    note: str = "critical-path unavailable"


@dataclass(frozen=True)
class TimingReport:
    """The timing report: top-N slowest tasks plus the critical-path seam."""

    top_slowest: list[TaskDuration] = field(default_factory=list)
    critical_path: CriticalPath = field(default_factory=CriticalPath)


def timing_report(
    artifact: dict | list,
    top_n: int = DEFAULT_TOP_N,
    *,
    baselines_path: Path | None = None,
) -> TimingReport:
    """Return the per-task timing report for one run.

    ``artifact`` is either a normalized ``bitbake-events.json`` dict or its
    already-parsed ``tasks`` list (per :func:`bakar.task_rollup._tasks_from`).
    A row missing ``completed`` (started-but-not-finished) or whose duration
    is negative is skipped without raising. The returned ``top_slowest`` list
    holds exactly ``top_n`` entries when at least that many valid-duration
    tasks exist, or every valid-duration task (unpadded) otherwise.

    Baseline context comes from :func:`bakar.task_timings.load_baselines`
    (``baselines_path`` threads through to it for tests; ``None`` uses the
    default on-disk location) - this reads the existing cross-build baseline
    store rather than recomputing a second one from the raw event deltas.
    """
    baselines = task_timings.load_baselines(baselines_path)

    # ``_tasks_from`` only handles a path or an already-parsed ``tasks`` list
    # (a dict artifact isn't Path/str/list, so passing it straight through
    # raises); unwrap the artifact's ``tasks`` key first, then let
    # ``_tasks_from`` do the list-verbatim/path-read extraction.
    tasks_source = artifact.get("tasks", []) if isinstance(artifact, dict) else artifact

    durations: list[TaskDuration] = []
    for row in _tasks_from(tasks_source):
        if not isinstance(row, dict):
            continue
        task = row.get("task")
        recipe = row.get("recipe")
        started = row.get("started")
        completed = row.get("completed")
        if not isinstance(task, str) or started is None or completed is None:
            continue
        try:
            duration = float(completed) - float(started)
        except TypeError, ValueError:
            continue
        if duration < 0:
            continue

        recipe_name = recipe if isinstance(recipe, str) else ""
        baseline = baselines.get(task_timings.baseline_key(recipe_name, task))
        mean, stddev = baseline if baseline is not None else (None, None)

        durations.append(
            TaskDuration(
                recipe=recipe_name,
                task=task,
                duration=duration,
                baseline_mean=mean,
                baseline_stddev=stddev,
            )
        )

    durations.sort(key=lambda d: d.duration, reverse=True)
    top_slowest = durations[:top_n] if top_n >= 0 else list(durations)

    return TimingReport(top_slowest=top_slowest, critical_path=CriticalPath())
