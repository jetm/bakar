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
import json
import math
from pathlib import Path
from typing import Any

DEFAULT_TIMINGS_PATH = Path.home() / ".local/state/bakar/task-timings.json"

SCHEMA_VERSION = 1


def load_baselines(path: Path | None = None) -> dict[str, tuple[float, float]]:
    """Load the timings file and return ``{taskname: (mean, stddev)}``.

    ``stddev`` is the sample standard deviation derived from Welford's
    accumulated ``m2`` and ``count``: ``sqrt(m2 / (count - 1))`` when
    ``count > 1``, else ``0.0``.

    Returns ``{}`` on any error (missing file, malformed JSON, missing keys)
    so callers never need to guard the read.
    """
    timings_path = path if path is not None else DEFAULT_TIMINGS_PATH
    try:
        with timings_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError, ValueError:
        return {}

    if not isinstance(data, dict):
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
        except TypeError, ValueError:
            continue
        stddev = math.sqrt(m2_f / (count_i - 1)) if count_i > 1 else 0.0
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
    ``tasks`` list whose entries carry ``task``, ``started`` and ``completed``
    epoch-second timestamps). For each task with both timestamps present and a
    non-negative duration, updates the baseline keyed by the bare ``task`` name
    via :func:`_welford_update`.

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
        durations.append((name, duration))

    if not durations:
        return

    timings_path.parent.mkdir(parents=True, exist_ok=True)

    # Open r+ when possible so the read and write share one locked handle;
    # fall back to creating the file when it does not yet exist.
    try:
        fh = timings_path.open("r+", encoding="utf-8")
    except OSError:
        fh = timings_path.open("w", encoding="utf-8")

    with fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            data: Any = json.loads(raw) if raw.strip() else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {"schema_version": SCHEMA_VERSION, "tasks": {}}
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            tasks = {}
            data["tasks"] = tasks
        data.setdefault("schema_version", SCHEMA_VERSION)

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
