"""Read bitbake's per-task buildstats tree.

bitbake writes one file per executed task under
``<TMPDIR>/buildstats/<timestamp>/<recipe>/<task>``, carrying the CPU time,
page-fault counts and IO syscall counts for that task. Nothing in bakar read
this before: the event log carries only ``started``/``completed``, so wall-clock
was the only dimension available and the CPU floor - the bound that says whether
a build is dependency-bound or throughput-bound at all - could not be computed.

Two decisions here are easy to get wrong and expensive when wrong.

**CPU time comes from the ``rusage`` lines, not the bare ``utime``.** A task file
carries both ``utime: 16`` and ``rusage ru_utime: 0.118937``. The first is clock
ticks, the second is seconds; reading the wrong one inflates CPU by roughly two
orders of magnitude and produces a CPU floor that looks like the binding
constraint on every build.

**Discovery separates three outcomes, not two.** A tree that was never found and
a build that recorded nothing are different answers - the first is a path
problem, the second is a real measurement - and a single "no tasks" result folds
them together. That distinction is the whole reason a caller can trust a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: bitbake's own whole-build summary, written directly in the timestamp
#: directory. Never a per-task record, and parsing it yields a bogus task row.
BUILD_SUMMARY_NAME = "build_stats"

#: Directory under TMPDIR holding per-run buildstats.
BUILDSTATS_DIR_NAME = "buildstats"


@dataclass(frozen=True)
class TaskStats:
    """One executed task's resource record.

    ``cpu_seconds`` sums the task's own and its children's user and system time.
    The children carry the bulk: bitbake forks the real work out to compilers
    and shells, so a task's own rusage alone understates it severely.
    """

    recipe: str
    task: str
    elapsed: float
    cpu_seconds: float
    minflt: int
    majflt: int
    syscalls: int
    write_bytes: int


@dataclass(frozen=True)
class BuildstatsRun:
    """The outcome of reading a buildstats tree.

    ``outcome`` is one of ``"absent"``, ``"empty"`` or ``"parsed"``. Callers
    MUST branch on it rather than on ``len(tasks)``, because ``absent`` and
    ``empty`` both yield no tasks and mean opposite things.
    """

    outcome: str
    note: str
    directory: Path | None = None
    tasks: list[TaskStats] = field(default_factory=list)

    @property
    def total_cpu_seconds(self) -> float:
        return sum(t.cpu_seconds for t in self.tasks)

    @property
    def total_elapsed_seconds(self) -> float:
        return sum(t.elapsed for t in self.tasks)


def _float_after_colon(line: str) -> float | None:
    """Parse the numeric tail of a ``key: value`` buildstats line."""
    _, _, rest = line.partition(":")
    token = rest.strip().split()
    if not token:
        return None
    try:
        return float(token[0])
    except ValueError:
        return None


#: Maps a buildstats line prefix to the internal key it contributes to.
#: Spelled out rather than derived, because the ``rusage``-vs-bare distinction
#: in the module docstring is exactly what a clever derivation would lose.
_FIELDS: dict[str, str] = {
    "Elapsed time:": "elapsed",
    "rusage ru_utime:": "ut",
    "rusage ru_stime:": "st",
    "Child rusage ru_utime:": "cut",
    "Child rusage ru_stime:": "cst",
    "rusage ru_minflt:": "minflt",
    "Child rusage ru_minflt:": "cminflt",
    "rusage ru_majflt:": "majflt",
    "Child rusage ru_majflt:": "cmajflt",
    "IO syscr:": "syscr",
    "IO syscw:": "syscw",
    "IO write_bytes:": "wb",
}


def parse_task_file(path: Path) -> dict[str, float]:
    """Parse one per-task buildstats file into its numeric fields.

    Unreadable files and unparseable lines are skipped rather than raised on: a
    buildstats tree is written concurrently with the build, so a truncated final
    record is an ordinary state rather than corruption.
    """
    found: dict[str, float] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return found
    for raw in text.splitlines():
        line = raw.strip()
        for prefix, key in _FIELDS.items():
            if line.startswith(prefix):
                value = _float_after_colon(line)
                if value is not None:
                    found[key] = value
                break
    return found


def _to_task_stats(recipe: str, task: str, d: dict[str, float]) -> TaskStats | None:
    """Build a record, or None when the file carried no elapsed time.

    ``elapsed`` is the one field with no sensible default. A task with no
    duration is a record bitbake had not finished writing, and defaulting it to
    zero would quietly pull the mean down rather than omit the row.
    """
    if "elapsed" not in d:
        return None
    return TaskStats(
        recipe=recipe,
        task=task,
        elapsed=d["elapsed"],
        cpu_seconds=d.get("ut", 0.0) + d.get("st", 0.0) + d.get("cut", 0.0) + d.get("cst", 0.0),
        minflt=int(d.get("minflt", 0.0) + d.get("cminflt", 0.0)),
        majflt=int(d.get("majflt", 0.0) + d.get("cmajflt", 0.0)),
        syscalls=int(d.get("syscr", 0.0) + d.get("syscw", 0.0)),
        write_bytes=int(d.get("wb", 0.0)),
    )


def latest_capture(tmpdir: Path | str) -> Path | None:
    """Return the newest timestamp directory under ``<tmpdir>/buildstats``.

    Timestamp directory names sort lexicographically in chronological order
    (``YYYYMMDDHHMMSS``), so the last one is the newest. A build directory
    accumulates one per run - 27 of them on one machine's tree here - so
    picking rather than assuming a single directory is required, not defensive.
    """
    root = Path(tmpdir) / BUILDSTATS_DIR_NAME
    try:
        captures = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return None
    return captures[-1] if captures else None


def read_run(tmpdir: Path | str) -> BuildstatsRun:
    """Read the newest buildstats capture under a build ``TMPDIR``.

    Pass ``BuildConfig.resolved_tmpdir``, not a guessed ``<workspace>/build/tmp``.
    A workspace can hold several build directories - one per machine - and the
    naive path is empty on exactly the workspace whose per-machine directories
    hold every capture, so a guess reports "no buildstats" on a tree full of
    them.
    """
    root = Path(tmpdir) / BUILDSTATS_DIR_NAME
    if not root.is_dir():
        return BuildstatsRun(outcome="absent", note=f"no buildstats tree at {root}")

    capture = latest_capture(tmpdir)
    if capture is None:
        return BuildstatsRun(
            outcome="empty",
            note=f"buildstats tree at {root} holds no capture directories",
            directory=None,
        )

    tasks: list[TaskStats] = []
    for recipe_dir in sorted(capture.iterdir()):
        if not recipe_dir.is_dir():
            continue
        for task_file in sorted(recipe_dir.iterdir()):
            if task_file.name == BUILD_SUMMARY_NAME or not task_file.is_file():
                continue
            record = _to_task_stats(recipe_dir.name, task_file.name, parse_task_file(task_file))
            if record is not None:
                tasks.append(record)

    if not tasks:
        return BuildstatsRun(
            outcome="empty",
            note=f"capture {capture} holds no parseable task records",
            directory=capture,
        )
    return BuildstatsRun(
        outcome="parsed",
        note=f"{len(tasks)} task records from {capture}",
        directory=capture,
        tasks=tasks,
    )
