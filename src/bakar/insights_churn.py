"""Per-task-type process-churn and I/O aggregation.

Split out of :mod:`bakar.insights_timing`. Aggregates buildstats fault,
syscall and write-byte counters per task type over the joined records,
reusing :func:`bakar.insights_joins._match_record` and
:func:`bakar.insights_joins._index_records` rather than re-implementing the
buildstats matching this module needs. Unlike the CPU floor
(:mod:`bakar.insights_timing`), this section is NOT withheld when the join
gate refuses (see :class:`TaskChurn`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar.insights_joins import _capture_phrase, _index_records, _match_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from bakar.buildstats import BuildstatsRun, TaskStats
    from bakar.insights_joins import _ExecutedTask

#: Column widths for the churn table, in the order the header names them. Held
#: as a constant rather than as formatter arguments because this project is
#: mid-way through a high-arity cleanup and a per-column parameter list is
#: exactly the signature that pass adds back.
#:
#: They sum to 79 plus a leading space, which keeps a row inside an 80-column
#: terminal, and the first column holds the longest real task name
#: (``do_package_write_rpm_setscene``, 29). A wider table is not a cosmetic
#: problem: Rich hard-wraps the overflow onto a second line, and a row split
#: across two lines is exactly the shape that gets read against the wrong
#: column heading.
CHURN_COLUMNS = ("task type", "tasks", "minflt", "majflt", "syscalls", "GB_wr")
CHURN_WIDTHS = (30, 5, 14, 10, 12, 8)

#: Bytes per gigabyte for the ``GB_wr`` column. Decimal, matching how the
#: reference analyser labelled the same column.
BYTES_PER_GB = 1_000_000_000

#: Stated beside every available churn table. Two things a reader cannot
#: recover from the numbers themselves: which rusage the counters come from,
#: and that the columns are read by field name.
#:
#: The self/child split is not a detail. On one real capture ``do_configure``
#: read 7,023,117 self minor faults against 214,243,919 CHILD minor faults -
#: the child accounts for 97% of the churn, because the work happens in spawned
#: configure and compiler subprocesses. A table summing only ``rusage ru_*``
#: under-reports by roughly 30x on exactly the task types this section exists
#: to characterize, so the choice is stated rather than left to be inferred.
CHURN_BASIS_NOTE = (
    "basis: each counter sums the task's OWN rusage and its CHILD rusage "
    "(bitbake forks the real work out, and the child carries ~97% of the faults on a real "
    "capture, so self-only counters under-report by roughly 30x). Fields are read by name from "
    "each buildstats file - 'rusage ru_minflt', 'Child rusage ru_majflt', 'IO syscr'/'IO syscw', "
    "'IO write_bytes' - never by column position"
)


def _churn_line(cells: tuple[str, ...]) -> str:
    """Lay one churn row - or the header - out on :data:`CHURN_WIDTHS`.

    The header and every data row go through this one function, so the two
    cannot drift into disagreeing about which column is which. The first column
    is left-justified and the numeric ones right-justified: right-justifying a
    task name would run it up against the ``tasks`` heading with no gap, which
    is how a reader ends up parsing a value against its neighbour's label.
    """
    parts = []
    for index, (cell, width) in enumerate(zip(cells, CHURN_WIDTHS, strict=True)):
        if index == 0:
            # Truncate rather than let the row grow past its width budget.
            # ``ljust`` does not cap, and the first column's stated maximum
            # (``do_package_write_rpm_setscene``, 29) is not the real one -
            # oe-core has at least five longer, up to
            # ``do_deploy_source_date_epoch_setscene`` at 36. An sstate-restored
            # build emits those routinely, which is bakar's default regime after
            # ``sstate-seed``. An over-long name pushed the row past the 80
            # columns the widths budget for, Rich hard-wrapped it, and the
            # numbers landed under the wrong headings - the exact misread this
            # table exists to prevent, and the one that once had ``majflt``
            # quoted as a task count. A clipped name is legible; a wrapped row
            # is actively misleading.
            parts.append(cell[: width - 1] + "~" if len(cell) > width else cell.ljust(width))
        else:
            parts.append(cell.rjust(width))
    return "".join(parts)


@dataclass(frozen=True)
class ChurnRow:
    """One task type's summed process-churn and I/O counters.

    ``minflt`` is process churn - pages faulted in without touching the disk,
    which is what a storm of short-lived autoconf probe processes produces.
    ``majflt`` and ``write_bytes`` are I/O. Keeping them in separate columns is
    the whole capability: the two profiles were indistinguishable on wall-clock
    alone, and it was the minor-to-major ratio that identified probe churn as a
    serial floor rather than a disk problem.
    """

    task: str
    tasks: int
    minflt: int
    majflt: int
    syscalls: int
    write_bytes: int


@dataclass(frozen=True)
class TaskChurn:
    """Per-task-type churn columns aggregated over the joined buildstats records.

    Only records matching an executed task contribute, for the same reason
    :func:`bakar.insights_joins._compute_join` restricts its CPU sum: a
    buildstats tree can carry rows from a previous run or a sibling machine's
    directory, and summing the tree wholesale credits them to this build.

    Unlike the CPU floor, this section is NOT withheld when the join gate
    refuses. The floor is a single build-wide bound, so a partial join makes it a
    bound for a different, smaller build; these are per-task-type aggregates, and
    a subset of ``do_compile`` records still describes ``do_compile``. What a
    partial join does cost is coverage, so ``covered``/``executed`` are stated in
    the note on every rendering rather than only on a refusal.
    """

    available: bool = False
    rows: list[ChurnRow] = field(default_factory=list)
    covered: int = 0
    executed: int = 0
    note: str = "task churn unavailable: no buildstats source supplied"
    basis_note: str | None = None

    def report_lines(self) -> list[str]:
        """Render the note, the basis, and the column table.

        Columns are emitted in :data:`CHURN_COLUMNS` order with the header
        printed from the same constant, so a reader and the formatter cannot
        disagree about which column is which - the failure this task's own
        history names, where the ``majflt`` column was quoted as a task count
        and ``GB_wr`` as cores-per-task.
        """
        lines = [f"  {self.note}"]
        if self.basis_note is not None:
            lines.append(f"  {self.basis_note}")
        if not self.available:
            return lines
        lines.append(" " + _churn_line(CHURN_COLUMNS))
        for row in self.rows:
            cells = (
                row.task,
                f"{row.tasks}",
                f"{row.minflt:,}",
                f"{row.majflt:,}",
                f"{row.syscalls:,}",
                f"{row.write_bytes / BYTES_PER_GB:.2f}",
            )
            lines.append(" " + _churn_line(cells))
        return lines


def _compute_churn(
    buildstats_source: Callable[[], BuildstatsRun],
    executed: list[_ExecutedTask],
) -> TaskChurn:
    """Aggregate churn counters per task type over the executed task set.

    ``executed`` is the timestamp-independent identity set
    (:data:`bakar.insights_joins._ExecutedTask`), the same one the join gate
    counts. The coverage stated in the note is only honest against that
    denominator: counting against the tasks that happened to carry a usable
    timestamp would report full coverage of a subset.

    Degrades with a note rather than raising, following
    :func:`bakar.insights_critical_path._compute_critical_path`'s precedent,
    and keeps ``absent``, ``empty`` and ``uncorrelated`` apart for the reason
    :func:`bakar.insights_joins._compute_join` does.

    Records are resolved through :func:`bakar.insights_joins._match_record`,
    the same matcher the join gate uses, so a record for a PF this run never
    executed reaches no counter here either. Walking the tree and testing the
    version-stripped key instead credits a stale ``busybox-1.37-r0`` row to
    the ``busybox-1.36.1-r0`` the run actually built, which inflates every
    counter in the row and reports a task count higher than the number of
    tasks that ran.

    Rows are ordered by minor faults descending, which puts the process-churn
    heavy task types at the top - the ordering that made the ``do_configure``
    profile visible in the first place.
    """
    try:
        run = buildstats_source()
    except Exception as exc:  # noqa: BLE001 - any buildstats-source failure degrades gracefully
        return TaskChurn(note=f"task churn unavailable: source failed ({exc})")

    if run.outcome == "absent":
        return TaskChurn(note=f"task churn unavailable: tree absent ({run.note})")
    if run.outcome == "uncorrelated":
        return TaskChurn(note=f"task churn unavailable: no capture belongs to this run ({run.note})")
    if run.outcome != "parsed":
        return TaskChurn(note=f"task churn unavailable: tree present but recorded nothing ({run.note})")

    exact, stripped = _index_records(run.tasks)
    # Distinct executed tasks covered, not records aggregated. Several executed
    # rows can resolve to one record key, so a record count would let ``covered``
    # exceed ``executed`` and turn the coverage note into a claim nobody can read.
    matched: set[tuple[str, str]] = set()
    for recipe, task in executed:
        key = _match_record(exact, stripped, recipe, task)
        if key is not None:
            matched.add(key)

    grouped: dict[str, list[TaskStats]] = {}
    for key in matched:
        for stat in exact[key]:
            grouped.setdefault(stat.task, []).append(stat)

    if not grouped:
        return TaskChurn(
            note=(
                f"task churn unavailable: none of the {len(run.tasks)} buildstats records match an "
                "executed task, so every counter would describe a different build"
            ),
        )

    rows = [
        ChurnRow(
            task=task,
            tasks=len(stats),
            minflt=sum(s.minflt for s in stats),
            majflt=sum(s.majflt for s in stats),
            syscalls=sum(s.syscalls for s in stats),
            write_bytes=sum(s.write_bytes for s in stats),
        )
        for task, stats in grouped.items()
    ]
    rows.sort(key=lambda r: r.minflt, reverse=True)
    return TaskChurn(
        available=True,
        rows=rows,
        covered=len(matched),
        executed=len(executed),
        note=(
            f"task churn over {len(matched)} of {len(executed)} executed tasks, aggregated into "
            f"{len(rows)} task types, {_capture_phrase(run)}"
        ),
        basis_note=CHURN_BASIS_NOTE,
    )
