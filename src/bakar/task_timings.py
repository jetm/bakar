"""Per-task-type build duration baselines.

bitbake reports per-task start/completed timestamps in the normalized
``bitbake-events.json`` artifact (see :mod:`bakar.eventlog`). Accumulating
those durations across builds, keyed by the bare task name (``do_compile``,
``do_install``, ...), gives a mean and stddev baseline the live UI can use to
estimate per-task progress and flag tasks running long.

The baselines persist as a small JSON file updated incrementally with
Welford's online algorithm, so a running mean/variance is maintained without
storing every sample. ``fcntl.flock`` guards the read-modify-write because
concurrent bakar processes may update the same file.

No ``bb`` module is imported; this reads only the already-normalized artifact.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

DEFAULT_TIMINGS_PATH = Path.home() / ".local/state/bakar/task-timings.json"

# Scoped baseline files live under this directory, one per build context.
TIMINGS_DIR = Path.home() / ".local/state/bakar/task-timings"


def timings_path_for(bsp_root: Path, machine: str, *, host_mode: bool = False) -> Path:
    """Return the baseline file scoped to one build context.

    Task durations are only comparable within the same workspace (same
    layers, distro config, patches), the same MACHINE (same target arch and
    tune), and the same execution mode (container vs host) - a peridio
    aarch64 container build must not train the baselines a variscite x86
    host build reads. The scope key is
    ``<sha256(realpath(bsp_root))[:16]>-<machine>-<container|host>``; the
    per-sample noise that remains inside one scope (ccache warmth, parallel
    load) is what the loose 2x/4x stuck thresholds absorb.
    """
    root_hash = hashlib.sha256(str(bsp_root.resolve()).encode()).hexdigest()[:16]
    mode = "host" if host_mode else "container"
    return TIMINGS_DIR / f"{root_hash}-{machine}-{mode}.json"


# v2: baselines keyed by "<recipe>:<task>" instead of the bare task name. A
# bare-task mean blended a 4-hour webkit do_compile with a 3-second one, so
# the prediction rarely matched the row it annotated. Older-version files are
# discarded on read (the data is cheap to re-accumulate).
SCHEMA_VERSION = 2

# Strips the "-<version>-r<rev>" (or bare "-<version>") suffix from a PF like
# "glibc-2.39-r0" so the baseline key survives recipe version bumps.
_PF_VERSION_RE = re.compile(r"-\d[^-]*(?:-r\d+)?$")


def baseline_key(recipe: str, task: str) -> str:
    """Return the stable baseline key ``"<recipe-sans-version>:<task>"``.

    ``recipe`` is the PF (``glibc-2.39-r0``); the version/revision suffix is
    stripped so a version bump keeps the history (``glibc:do_compile``). A PF
    that does not match the version pattern is used verbatim, which keeps the
    key deterministic on both the write (``update_from_events``) and lookup
    (``BuildUIState``) sides.
    """
    pn = _PF_VERSION_RE.sub("", recipe) if recipe else ""
    return f"{pn or recipe}:{task}"


def load_baselines(path: Path | None = None) -> dict[str, tuple[float, float]]:
    """Load the timings file and return ``{"recipe:task": (mean, stddev)}``.

    ``stddev`` is the sample standard deviation derived from Welford's
    accumulated ``m2`` and ``count``: ``sqrt(m2 / (count - 1))`` when
    ``count > 1``, else ``0.0``.

    Returns ``{}`` on any error (missing file, malformed JSON, missing keys)
    or when the file carries a different ``schema_version``, so callers never
    need to guard the read.
    """
    timings_path = path if path is not None else DEFAULT_TIMINGS_PATH
    try:
        with timings_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError, ValueError:
        return {}

    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {}
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return {}

    baselines: dict[str, tuple[float, float]] = {}
    for name, entry in tasks.items():
        if not isinstance(entry, dict):
            continue
        count = entry.get("count", 0)
        mean = entry.get("mean", 0.0)
        m2 = entry.get("m2", 0.0)
        try:
            count_i = int(count)
            mean_f = float(mean)
            m2_f = float(m2)
            if not (math.isfinite(mean_f) and math.isfinite(m2_f)):
                continue
            variance = m2_f / (count_i - 1) if count_i > 1 else 0.0
            stddev = math.sqrt(variance) if variance > 0 else 0.0
        except TypeError, ValueError, OverflowError:
            continue
        baselines[name] = (mean_f, stddev)
    return baselines


def _welford_update(entry: dict, x: float) -> None:
    """In-place Welford update of ``count``, ``mean``, ``m2``, ``min``, ``max``.

    Missing keys default to ``0``/``0.0``/``inf`` so a sparse or partially
    written existing entry updates without crashing.
    """
    count = entry.get("count", 0) + 1
    mean = entry.get("mean", 0.0)
    m2 = entry.get("m2", 0.0)

    delta = x - mean
    mean += delta / count
    delta2 = x - mean
    m2 += delta * delta2

    entry["count"] = count
    entry["mean"] = mean
    entry["m2"] = m2
    entry["min"] = min(entry.get("min", math.inf), x)
    entry["max"] = max(entry.get("max", -math.inf), x)


def update_from_events(events_json: Path, timings_path: Path) -> None:
    """Fold per-task durations from a normalized events artifact into the file.

    Reads ``events_json`` (the ``bitbake-events.json`` schema: a top-level
    ``tasks`` list whose entries carry ``recipe``, ``task``, ``started`` and
    ``completed`` epoch-second timestamps). For each task with both timestamps
    present and a non-negative duration, updates the baseline keyed by
    :func:`baseline_key` (``"<recipe-sans-version>:<task>"``) via
    :func:`_welford_update`.

    The timings file is updated under an exclusive ``flock`` so concurrent
    bakar processes cannot corrupt it. The parent directory is created if
    absent, and a missing or malformed existing file is tolerated by starting
    from an empty baseline.
    """
    try:
        with events_json.open("r", encoding="utf-8") as fh:
            artifact = json.load(fh)
    except OSError, ValueError:
        return
    if not isinstance(artifact, dict):
        return
    task_rows = artifact.get("tasks")
    if not isinstance(task_rows, list):
        return

    durations: list[tuple[str, float]] = []
    for row in task_rows:
        if not isinstance(row, dict):
            continue
        name = row.get("task")
        recipe = row.get("recipe")
        started = row.get("started")
        completed = row.get("completed")
        if not isinstance(name, str) or started is None or completed is None:
            continue
        try:
            duration = float(completed) - float(started)
        except TypeError, ValueError:
            continue
        if duration < 0:
            continue
        durations.append((baseline_key(recipe if isinstance(recipe, str) else "", name), duration))

    if not durations:
        return

    timings_path.parent.mkdir(parents=True, exist_ok=True)

    # Open r+ when possible so the read and write share one locked handle.
    # Fall back to "a+" (create-without-truncate) so the fallback handle is
    # readable (not write-only) and the file creation does not race with the
    # flock: a+ creates the file but never truncates it, so two concurrent
    # first-run writers do not discard each other's samples.
    try:
        fh = timings_path.open("r+", encoding="utf-8")
    except OSError:
        fh = timings_path.open("a+", encoding="utf-8")

    with fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            data: Any = json.loads(raw) if raw.strip() else None
        except ValueError:
            data = None
        # A different schema_version (e.g. v1 bare-task keys) is discarded
        # wholesale: the keying changed, and re-accumulating is cheap.
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            data = {"schema_version": SCHEMA_VERSION, "tasks": {}}
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            tasks = {}
            data["tasks"] = tasks

        for name, duration in durations:
            entry = tasks.get(name)
            if not isinstance(entry, dict):
                entry = {}
                tasks[name] = entry
            _welford_update(entry, duration)

        fh.seek(0)
        fh.truncate()
        json.dump(data, fh)
        fh.flush()
        fcntl.flock(fh, fcntl.LOCK_UN)
