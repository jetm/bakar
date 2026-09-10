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

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

    ``outcome`` is one of ``"absent"``, ``"empty"``, ``"uncorrelated"`` or
    ``"parsed"``. Callers MUST branch on it rather than on ``len(tasks)``,
    because the first three all yield no tasks and mean different things: no
    tree at all, a tree that recorded nothing, and a tree holding only captures
    belonging to some other build.
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
    """Parse the numeric tail of a ``key: value`` buildstats line.

    ``None`` for anything that is not a finite number. ``float()`` accepts
    ``nan`` and ``inf`` as ordinary literals, and a buildstats file is written
    concurrently with the build by a process that can be killed mid-line, so a
    garbled token is an ordinary state rather than a theoretical one. Letting
    either through propagates into the CPU floor and the headroom percentage,
    where it renders as ``nan`` rather than as a refusal, and ``int(inf)``
    raises out of the whole read.
    """
    _, _, rest = line.partition(":")
    token = rest.strip().split()
    if not token:
        return None
    try:
        value = float(token[0])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


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


#: The CPU fields a record MUST carry to be usable. bitbake writes a task file
#: incrementally - ``Elapsed time`` lands before the ``rusage`` block and the
#: ``Child rusage`` block lands last - so a file read while the build is still
#: running can carry a duration and no CPU at all. Defaulting those to zero made
#: such a record a valid ``TaskStats`` that JOINED, which let the 95% join gate
#: pass while the CPU floor it gates silently understated the build.
_REQUIRED_CPU: tuple[str, ...] = ("ut", "st", "cut", "cst")


def _to_task_stats(recipe: str, task: str, d: dict[str, float]) -> TaskStats | None:
    """Build a record, or ``None`` when the file is incomplete or impossible.

    Three rejections, and the asymmetry between the first two is deliberate:

    - **No ``elapsed``.** A task with no duration is a record bitbake had not
      finished writing, and defaulting it to zero would quietly pull the mean
      down rather than omit the row.
    - **Any missing CPU field, or every CPU field exactly zero.** Absence and a
      legitimate zero are indistinguishable once defaulted, and here they are
      not the same thing: a task that consumed no CPU does not exist, so a zero
      in this position is always a record that was not finished. Requiring
      presence alone left the other half of that invariant unenforced - a file
      caught mid-write with ``Elapsed time`` and four ``0.0`` rusage lines
      carries every required key and still measured nothing. Such a record must
      not reach the join numerator, because joining it certifies CPU seconds
      nobody measured while raising the rate that gates them.
    - **Any negative value.** Every field here counts time, faults, syscalls or
      bytes, none of which can run backwards. A negative CPU component yields a
      negative CPU floor, which reads as enormous headroom.

    The churn counters are deliberately NOT required, and that is the one place
    this diverges from the CPU rule above. A zero churn counter is legitimate -
    a task can genuinely record 0 major faults, and many do - so absence and
    zero are not distinguishable there by inspection the way they are for CPU.
    Requiring them would drop real records and depress the join rate over a
    column that only describes, while the gated number (the CPU floor) is
    computed from fields this function does require.
    """
    if "elapsed" not in d:
        return None
    if any(key not in d for key in _REQUIRED_CPU):
        return None
    if all(d[key] == 0.0 for key in _REQUIRED_CPU):
        return None
    if any(value < 0 for value in d.values()):
        return None
    return TaskStats(
        recipe=recipe,
        task=task,
        elapsed=d["elapsed"],
        cpu_seconds=sum(d[key] for key in _REQUIRED_CPU),
        minflt=int(d.get("minflt", 0.0) + d.get("cminflt", 0.0)),
        majflt=int(d.get("majflt", 0.0) + d.get("cmajflt", 0.0)),
        syscalls=int(d.get("syscr", 0.0) + d.get("syscw", 0.0)),
        write_bytes=int(d.get("wb", 0.0)),
    )


#: Slack either side of the run's own build window when correlating a capture
#: with it. bitbake creates the capture directory as the build starts, so the
#: timestamp lands inside the window; the slack absorbs clock granularity and
#: the ordering between the directory's creation and the ``BuildStarted`` event,
#: not an arbitrary "close enough".
CAPTURE_SLACK_SECONDS = 300.0


def _named_epoch(path: Path) -> float | None:
    """Parse a ``YYYYMMDDHHMMSS`` capture-directory name as UTC, or ``None``.

    This reading is the build's START, which is what correlation actually wants,
    but the name carries no timezone. bitbake writes it from the build
    container's clock, and kas-container runs UTC while this analysing host runs
    UTC-6 - so reading it as LOCAL time put every capture exactly 6.00 h from its
    own run's window and nothing ever correlated. Reading it as UTC fixes this
    fleet and would break one whose builds run in local time, which is what
    :func:`_mtime_epoch` backs up.

    ``None`` when the name is not ``YYYYMMDDHHMMSS``. That shape check is the
    only thing standing between a scratch directory and a run's provenance, so a
    capture that fails it never correlates by any reading.
    """
    try:
        named = datetime.strptime(path.name, "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None
    return named


def _mtime_epoch(path: Path) -> float:
    """The capture directory's mtime, or ``-inf`` when it cannot be read.

    A true epoch needing no timezone guess, which is why it can place a capture
    a local-time-writing host named hours out of its own window. It is NOT the
    build's start: bitbake writes recipe subdirectories throughout the build and
    each one bumps the parent's mtime, so this lands near the build's END.
    Measured on a real 6-minute capture, name 14:20:35 against mtime 14:26:30 -
    which is why :func:`select_capture` ranks it in a separate tier rather than
    pooling it with the name.

    ``-inf`` never falls inside a window, so an unreadable directory declines to
    correlate instead of raising out of capture selection.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def select_capture(tmpdir: Path | str, window: tuple[float, float]) -> Path | None:
    """Return the capture whose timestamp falls inside a run's build window.

    Selecting the NEWEST capture instead is the defect this exists to close:
    reporting on an older run then joins it against a different build's records,
    and because consecutive builds of one target execute a near-identical
    ``(PN, task)`` set, the join gate passes at close to 100% over records the
    run never produced. Provenance is checked, not assumed (design D5).

    ``None`` when nothing correlates - the caller MUST refuse rather than fall
    back to the newest.
    """
    started, completed = window
    root = Path(tmpdir) / BUILDSTATS_DIR_NAME
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return None
    lo = started - CAPTURE_SLACK_SECONDS
    hi = completed + CAPTURE_SLACK_SECONDS

    named_matches: list[tuple[float, str, Path]] = []
    mtime_matches: list[Path] = []
    for p in candidates:
        named = _named_epoch(p)
        if named is None:
            # Not a ``YYYYMMDDHHMMSS`` directory, so it cannot be placed in time
            # at all - not even by mtime, which would let a scratch directory
            # touched during the build read as this run's provenance.
            continue
        if lo <= named <= hi:
            named_matches.append((abs(named - started), p.name, p))
        elif lo <= _mtime_epoch(p) <= hi:
            mtime_matches.append(p)

    if named_matches:
        # Closest to the build's START, not the last one inside the window. Two
        # builds a couple of minutes apart both fall inside the other's window
        # once the slack is applied, and taking the latest match then hands an
        # earlier run the later build's capture - the same wrong-build join this
        # function exists to prevent, reintroduced by the tolerance. bitbake
        # creates the directory as the build starts, so proximity to ``started``
        # is what identifies it.
        return min(named_matches)[2]

    # Nothing named this run's window, so the only remaining evidence is mtime -
    # and mtime is weaker in a way that ranking hides. It is near the build's
    # END, not its start, so an ambiguous set cannot be resolved by "closest to
    # started": the previous build's END sits nearer this run's start than this
    # run's own end does, and ranking therefore prefers the wrong build. One
    # match is the local-time-writing host this fallback exists for; more than
    # one is provenance nobody can settle, and D5 refuses rather than guesses.
    return mtime_matches[0] if len(mtime_matches) == 1 else None


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


def read_run(tmpdir: Path | str, *, window: tuple[float, float] | None = None) -> BuildstatsRun:
    """Read a buildstats capture under a build ``TMPDIR``.

    Pass ``BuildConfig.resolved_tmpdir``, not a guessed ``<workspace>/build/tmp``.
    A workspace can hold several build directories - one per machine - and the
    naive path is empty on exactly the workspace whose per-machine directories
    hold every capture, so a guess reports "no buildstats" on a tree full of
    them.

    ``window`` is the run's own ``(build started, build completed)`` epoch pair.
    Supply it whenever the caller knows which run it is reporting on: the
    capture is then chosen by correlating with that window
    (:func:`select_capture`), and a run with no correlatable capture yields
    outcome ``"uncorrelated"`` rather than a different build's records. Omitting
    it keeps the older newest-capture behaviour, which is right only for a
    caller that genuinely wants whatever ran last.
    """
    root = Path(tmpdir) / BUILDSTATS_DIR_NAME
    if not root.is_dir():
        return BuildstatsRun(outcome="absent", note=f"no buildstats tree at {root}")

    # Emptiness is settled BEFORE correlation, because ``select_capture``
    # returns None for both "the root holds nothing" and "nothing here belongs
    # to this run" - and those call for opposite responses. Deciding
    # "uncorrelated" off a bare None told a reader that some other build's
    # captures were sitting there when the directory was in fact empty.
    try:
        has_captures = any(p.is_dir() for p in root.iterdir())
    except OSError as exc:
        return BuildstatsRun(outcome="absent", note=f"buildstats tree at {root} could not be read ({exc})")
    if not has_captures:
        return BuildstatsRun(
            outcome="empty",
            note=f"buildstats tree at {root} holds no capture directories",
            directory=None,
        )

    capture = latest_capture(tmpdir) if window is None else select_capture(tmpdir, window)
    if capture is None:
        if window is None:
            # The root held captures a moment ago and holds none now, which is
            # the concurrent-build race rather than an empty tree.
            return BuildstatsRun(
                outcome="empty",
                note=f"buildstats tree at {root} lost its capture directories mid-read",
                directory=None,
            )
        return BuildstatsRun(
            outcome="uncorrelated",
            note=(
                f"no buildstats capture under {root} falls within this run's build window "
                f"({window[0]:.0f}-{window[1]:.0f} epoch, {CAPTURE_SLACK_SECONDS:.0f}s slack) - "
                "the newest capture is deliberately not substituted, because a different build's "
                "records join at near 100% and read exactly like this run's own"
            ),
        )

    tasks: list[TaskStats] = []
    incomplete = 0
    vanished = 0
    # Both levels of the walk are guarded for the reason ``parse_task_file`` and
    # ``latest_capture`` already are: the tree is written concurrently with the
    # build, so a recipe directory can be created, rotated or removed between
    # the listing and the read. An unguarded walk raises out of here instead of
    # degrading, which D2 forbids.
    try:
        recipe_dirs = sorted(capture.iterdir())
    except OSError as exc:
        return BuildstatsRun(
            outcome="empty",
            note=f"capture {capture} could not be read ({exc})",
            directory=capture,
        )
    for recipe_dir in recipe_dirs:
        try:
            if not recipe_dir.is_dir():
                continue
            task_files = sorted(recipe_dir.iterdir())
        except OSError:
            vanished += 1
            continue
        for task_file in task_files:
            if task_file.name == BUILD_SUMMARY_NAME or not task_file.is_file():
                continue
            record = _to_task_stats(recipe_dir.name, task_file.name, parse_task_file(task_file))
            if record is None:
                incomplete += 1
            else:
                tasks.append(record)

    detail = ""
    if incomplete:
        detail += f", {incomplete} incomplete or invalid records skipped"
    if vanished:
        detail += f", {vanished} recipe directories unreadable mid-walk"

    if not tasks:
        return BuildstatsRun(
            outcome="empty",
            note=f"capture {capture} holds no parseable task records{detail}",
            directory=capture,
        )
    return BuildstatsRun(
        outcome="parsed",
        note=f"{len(tasks)} task records from {capture}{detail}",
        directory=capture,
        tasks=tasks,
    )
