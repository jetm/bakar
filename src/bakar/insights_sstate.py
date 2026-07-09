"""Per-recipe sstate hit/miss report.

bitbake's setscene tasks (``*_setscene``) probe the sstate cache before a
recipe's real task runs: a probe that succeeds is a cache *hit* (the recipe
skips real work), a probe that comes back empty is a cache *miss* (the recipe
falls through to the real task). The normalized ``bitbake-events.json``
artifact (see :mod:`bakar.eventlog`) records each setscene probe as an
ordinary row in ``tasks`` - ``TaskSucceeded`` means the sstate object was
found (hit), ``TaskFailedSilent`` means it was not (miss, per
``eventlog.normalize``'s docstring: "mirrors bitbake's ``BBUIHelper``").

This module groups those rows by recipe and reports hits, misses, and a miss
ratio, answering "what is blowing my sstate cache?" per-recipe rather than
the one aggregate ``Sstate summary: Wanted N Found M Missed P`` line bitbake
prints today.

Like :mod:`bakar.task_rollup`, this is a single-run, no-persistence pure
function: no filesystem or subprocess access happens inside
:func:`sstate_report` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bakar.task_rollup import _tasks_from

# Row outcome for a *_setscene task that found its sstate object (cache hit).
_HIT_OUTCOME = "succeeded"
# Row outcome for a *_setscene task that did NOT find its sstate object (cache
# miss); bitbake reports this as TaskFailedSilent, not a real failure.
_MISS_OUTCOME = "failed_silent"

_SETSCENE_SUFFIX = "_setscene"

NO_DATA_MESSAGE = "no sstate data found for run"


@dataclass(frozen=True)
class SstateRecipeStat:
    """Hit/miss counts for one recipe's setscene tasks."""

    recipe: str
    hits: int = 0
    misses: int = 0

    @property
    def miss_ratio(self) -> float:
        """Return ``misses / (hits + misses)``, or 0.0 when there is no data."""
        total = self.hits + self.misses
        return self.misses / total if total else 0.0


@dataclass(frozen=True)
class SstateReport:
    """The per-recipe sstate report for one run.

    ``recipes`` is sorted by descending miss count. When the run's ``tasks``
    contain no ``*_setscene`` rows at all, ``recipes`` is empty and
    ``message`` explains why (:data:`NO_DATA_MESSAGE`) rather than leaving
    callers to guess whether "no rows" means "no misses" or "no data".
    """

    recipes: list[SstateRecipeStat] = field(default_factory=list)
    message: str | None = None


def sstate_report(artifact: dict | list) -> SstateReport:
    """Compute the per-recipe sstate hit/miss report for one run.

    ``artifact`` is either the normalized artifact dict (as returned by
    :func:`bakar.eventlog.normalize`) or its already-extracted ``tasks``
    list. Tasks-list extraction is delegated to
    :func:`bakar.task_rollup._tasks_from` rather than re-parsed here.

    A row counts toward a recipe's hits when its ``task`` ends with
    ``_setscene`` and its ``outcome`` is ``"succeeded"``, and toward misses
    when the outcome is ``"failed_silent"``. Rows with any other outcome
    (still running, or a genuine task failure) carry no hit/miss signal and
    are excluded. When no ``*_setscene`` rows resolve to a hit or a miss,
    the returned report is empty with :data:`NO_DATA_MESSAGE` explaining why.
    """
    tasks = artifact.get("tasks", []) if isinstance(artifact, dict) else artifact

    hits: dict[str, int] = {}
    misses: dict[str, int] = {}

    for row in _tasks_from(tasks):
        if not isinstance(row, dict):
            continue
        task = row.get("task")
        if not isinstance(task, str) or not task.endswith(_SETSCENE_SUFFIX):
            continue
        recipe = row.get("recipe")
        if not isinstance(recipe, str):
            continue

        outcome = row.get("outcome")
        if outcome == _HIT_OUTCOME:
            hits[recipe] = hits.get(recipe, 0) + 1
        elif outcome == _MISS_OUTCOME:
            misses[recipe] = misses.get(recipe, 0) + 1
        # Any other outcome (None/"failed") is neither a hit nor a miss signal.

    if not hits and not misses:
        return SstateReport(recipes=[], message=NO_DATA_MESSAGE)

    recipes = sorted(
        (
            SstateRecipeStat(recipe=name, hits=hits.get(name, 0), misses=misses.get(name, 0))
            for name in hits.keys() | misses.keys()
        ),
        key=lambda stat: stat.misses,
        reverse=True,
    )
    return SstateReport(recipes=recipes)
