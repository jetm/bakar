"""Per-run task-family wall-time rollup.

bitbake records per-task start/completed timestamps in the normalized
``bitbake-events.json`` artifact (see :mod:`bakar.eventlog`). Summing each
task's wall-time (``completed - started``) into a small set of task families
(``do_compile``, ``do_configure``, ``do_install``, ``do_fetch``, ``other``)
quantifies where a build's wall-clock actually goes - e.g. a ``do_configure``
storm versus real compile time.

Unlike :mod:`bakar.task_timings`, which accumulates a cross-build baseline
under a file lock, this is a single-run read with no persistence: it takes one
artifact (or its parsed ``tasks`` list) and returns the rollup for that run.

A best-effort ``go_compile_seconds`` total is also returned: the summed
``do_compile`` wall-time for tasks whose ``recipe`` matches a Go recipe-name
signal. It is a subset of the ``do_compile`` family total and never alters it.

No ``bb`` module is imported; this reads only the already-normalized artifact,
mirroring :mod:`bakar.task_timings`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Bare task names that get their own family; everything else is ``other``.
KNOWN_FAMILIES = ("do_compile", "do_configure", "do_install", "do_fetch")
OTHER_FAMILY = "other"
ALL_FAMILIES = (*KNOWN_FAMILIES, OTHER_FAMILY)


@dataclass(frozen=True)
class FamilyStat:
    """Summed wall-time (seconds) and task count for one task family."""

    seconds: float = 0.0
    count: int = 0


@dataclass(frozen=True)
class TaskRollup:
    """A single run's task-family wall-time rollup.

    ``families`` always carries every entry in :data:`ALL_FAMILIES` (zeroed
    when a family had no usable task), so the rendering side has a stable
    shape. ``go_compile_seconds`` is the best-effort Go subset of the
    ``do_compile`` family total.
    """

    families: dict[str, FamilyStat] = field(default_factory=dict)
    go_compile_seconds: float = 0.0


def _is_go_recipe(recipe: str) -> bool:
    """Return True when ``recipe`` matches a Go recipe-name signal.

    The recipe name equals ``go`` or ``golang``, or is prefixed by ``go-`` or
    ``golang-``. Prefix/exact match, NOT a bare substring, so
    ``gobject-introspection`` and ``google-*`` do not match.
    """
    return recipe in ("go", "golang") or recipe.startswith(("go-", "golang-"))


def _tasks_from(source: Path | str | list) -> list:
    """Return the task rows from an artifact path or an already-parsed list.

    A list is used verbatim. A path is read as the ``bitbake-events.json``
    schema (a dict with a top-level ``tasks`` list). Any error - missing file,
    malformed JSON, wrong shape - yields an empty list so callers never guard
    the read, mirroring :func:`bakar.task_timings.update_from_events`.
    """
    if isinstance(source, list):
        return source
    try:
        with Path(source).open("r", encoding="utf-8") as fh:
            artifact = json.load(fh)
    except OSError, ValueError:
        return []
    if not isinstance(artifact, dict):
        return []
    rows = artifact.get("tasks")
    return rows if isinstance(rows, list) else []


def compute_task_rollup(source: Path | str | list) -> TaskRollup:
    """Compute the per-family wall-time rollup for one run.

    ``source`` is either the path to a normalized ``bitbake-events.json`` or
    its already-parsed ``tasks`` list. Each task row is expected to carry
    ``recipe``, ``task``, ``started`` and ``completed`` (epoch seconds).

    A task missing ``started`` or ``completed``, or one whose duration is
    negative, is excluded without raising. The returned ``families`` dict
    always carries all of :data:`ALL_FAMILIES`.
    """
    seconds: dict[str, float] = dict.fromkeys(ALL_FAMILIES, 0.0)
    counts: dict[str, int] = dict.fromkeys(ALL_FAMILIES, 0)
    go_compile_seconds = 0.0

    for row in _tasks_from(source):
        if not isinstance(row, dict):
            continue
        task = row.get("task")
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

        family = task if task in KNOWN_FAMILIES else OTHER_FAMILY
        seconds[family] += duration
        counts[family] += 1

        if family == "do_compile":
            recipe = row.get("recipe")
            if isinstance(recipe, str) and _is_go_recipe(recipe):
                go_compile_seconds += duration

    families = {name: FamilyStat(seconds[name], counts[name]) for name in ALL_FAMILIES}
    return TaskRollup(families=families, go_compile_seconds=go_compile_seconds)
