"""Tests for :mod:`bakar.eventlog` against the committed hermetic fixture.

The fixture (``tests/fixtures/bitbake_eventlog.json``) mimics bitbake's
``BB_DEFAULT_EVENTLOG`` output: JSON Lines of ``{"class","vars"}`` where
``vars`` is base64(pickle(event)), plus one ``{"allvariables": ...}`` line, an
unrecognized event class, and a deliberately truncated trailing line. These
tests prove the reader decodes the recognized events without ever importing
``bb``, skips what it should skip, and produces the schema the downstream
``bitbake-events.json`` contract depends on.
"""

from __future__ import annotations

import base64
import json
import pickle
import sys
from pathlib import Path

import pytest

from bakar import eventlog

FIXTURE = Path(__file__).parent / "fixtures" / "bitbake_eventlog.json"


class _StubStats:
    """Stand-in for bb.runqueue's runQueueStats (decoded via the stub unpickler)."""


class _StubEvent:
    """Stand-in for a bitbake event carrying a ``stats`` attribute."""


class _StubDiskUsageSample:
    """Stand-in for bb.event.DiskUsageSample (nested inside MonitorDiskEvent.disk_usage)."""

    def __init__(self, available_bytes: int, free_bytes: int, total_bytes: int) -> None:
        self.available_bytes = available_bytes
        self.free_bytes = free_bytes
        self.total_bytes = total_bytes


def _encode_event(obj: object) -> str:
    """base64(pickle(obj)) - the wire format of an event log ``vars`` payload."""
    return base64.b64encode(pickle.dumps(obj)).decode("ascii")


@pytest.mark.unit
def test_normalize_decodes_task_fields() -> None:
    """TaskFailed/TaskStarted decode to recipe/task/logfile from the pickle."""
    artifact = eventlog.normalize(FIXTURE)
    tasks = {(t["recipe"], t["task"]): t for t in artifact["tasks"]}

    started = tasks[("busybox-1.36.1-r0", "do_compile")]
    assert started["pid"] == 4242
    assert started["logfile"] == ("/work/build/tmp/work/cortexa53/busybox/1.36.1-r0/temp/log.do_compile.4242")

    failed = tasks[("linux-imx-6.12-r0", "do_compile")]
    assert failed["outcome"] == "failed"
    assert failed["logfile"] == ("/work/build/tmp/work/imx95/linux-imx/6.12-r0/temp/log.do_compile.5151")


@pytest.mark.unit
def test_normalize_captures_runqueue_total(tmp_path: Path) -> None:
    """normalize records the runqueue total/completed/active from the latest
    runQueueTaskStarted.stats, so consumers can show how far the build has to go."""
    stats = _StubStats()
    stats.total = 120  # type: ignore[attr-defined]
    stats.completed = 80  # type: ignore[attr-defined]
    stats.active = 3  # type: ignore[attr-defined]
    stats.setscene_total = 0  # type: ignore[attr-defined]
    event = _StubEvent()
    event.stats = stats  # type: ignore[attr-defined]

    log = tmp_path / "el.json"
    log.write_text(
        json.dumps({"class": "bb.runqueue.runQueueTaskStarted", "vars": _encode_event(event)}) + "\n",
        encoding="utf-8",
    )

    build = eventlog.normalize(log)["build"]
    assert build["tasks_total"] == 120
    assert build["tasks_completed"] == 80
    assert build["tasks_active"] == 3


@pytest.mark.unit
def test_normalize_does_not_import_bb() -> None:
    """The no-bitbake-dependency guarantee: ``bb`` must not be imported."""
    # Guard against a polluted interpreter from an earlier import elsewhere.
    bb_before = {name for name in sys.modules if name == "bb" or name.startswith("bb.")}
    assert not bb_before, f"bb already imported before the call: {bb_before}"

    eventlog.normalize(FIXTURE)

    bb_after = {name for name in sys.modules if name == "bb" or name.startswith("bb.")}
    assert not bb_after, f"normalize() imported bb modules: {bb_after}"


@pytest.mark.unit
def test_unknown_event_class_is_skipped() -> None:
    """The unrecognized class line contributes no task and raises nothing."""
    artifact = eventlog.normalize(FIXTURE)

    # The fixture's recognized task lines: busybox (started+succeeded merge to
    # one row), linux-imx (failed), zlib (failed_silent). The unknown class
    # and allvariables lines must NOT add rows.
    assert len(artifact["tasks"]) == 3
    recipes = {t["recipe"] for t in artifact["tasks"]}
    assert recipes == {"busybox-1.36.1-r0", "linux-imx-6.12-r0", "zlib-1.3-r0"}


@pytest.mark.unit
def test_allvariables_line_is_not_an_event() -> None:
    """The variable dump must not be parsed as a task or failure."""
    artifact = eventlog.normalize(FIXTURE)

    for task in artifact["tasks"]:
        assert task["recipe"] is not None
        assert "allvariables" not in (task["recipe"] or "")
    # No failure should originate from the allvariables dump either.
    assert all("MACHINE" not in (f["recipe"] or "") for f in artifact["failures"])


@pytest.mark.unit
def test_truncated_trailing_line_is_tolerated() -> None:
    """A truncated final line is skipped; the artifact still normalizes."""
    # Confirm the fixture really does end with a truncated (non-JSON) tail so
    # the test asserts something real.
    last_line = FIXTURE.read_text(encoding="utf-8").splitlines()[-1]
    with pytest.raises(ValueError):
        json.loads(last_line)

    artifact = eventlog.normalize(FIXTURE)
    assert artifact["schema_version"] == eventlog.SCHEMA_VERSION
    assert len(artifact["tasks"]) == 3


@pytest.mark.unit
def test_normalize_returns_schema_keys() -> None:
    """The artifact has exactly the contract's top-level keys."""
    artifact = eventlog.normalize(FIXTURE)
    assert artifact["schema_version"] == eventlog.SCHEMA_VERSION
    assert set(artifact) == {"schema_version", "build", "tasks", "setscene", "failures", "psi", "disk"}


@pytest.mark.unit
def test_normalize_prunes_dead_schema_fields() -> None:
    """The pruned build.preset/build.release/setscene.per_recipe fields must
    stay gone, and SCHEMA_VERSION must reflect the shape change (3 -> 4)."""
    artifact = eventlog.normalize(FIXTURE)

    assert eventlog.SCHEMA_VERSION == 4
    assert artifact["schema_version"] == 4
    assert "preset" not in artifact["build"]
    assert "release" not in artifact["build"]
    assert "per_recipe" not in artifact["setscene"]


@pytest.mark.unit
def test_failed_silent_in_tasks_but_not_failures() -> None:
    """TaskFailedSilent is recorded in tasks but excluded from failures."""
    artifact = eventlog.normalize(FIXTURE)

    silent = [t for t in artifact["tasks"] if t["outcome"] == "failed_silent"]
    assert len(silent) == 1
    assert silent[0]["recipe"] == "zlib-1.3-r0"
    assert silent[0]["task"] == "do_fetch_setscene"

    failure_recipes = {f["recipe"] for f in artifact["failures"]}
    assert "zlib-1.3-r0" not in failure_recipes
    assert failure_recipes == {"linux-imx-6.12-r0"}


@pytest.mark.unit
def test_omitted_optional_field_emitted_as_null() -> None:
    """An absent optional field surfaces as ``None``, never dropped."""
    artifact = eventlog.normalize(FIXTURE)

    # The failed task carries no TaskStarted line, so ``started`` is present
    # but null rather than missing from the row.
    failed = next(t for t in artifact["tasks"] if t["outcome"] == "failed")
    assert "started" in failed
    assert failed["started"] is None
    assert "pid" in failed
    assert failed["pid"] is None


@pytest.mark.unit
def test_metadata_event_classifies_cache_backend(tmp_path: Path) -> None:
    """A MetadataEvent with type ``bakar-cache-backend`` merges into the
    matching task row (matched via _package/_task) and sets cache_backend
    from _localdata - the positive classification case."""
    started = _StubEvent()
    started._package = "busybox-1.36.1-r0"  # type: ignore[attr-defined]
    started._task = "do_compile"  # type: ignore[attr-defined]
    started.time = 100.0  # type: ignore[attr-defined]

    classified = _StubEvent()
    classified._package = "busybox-1.36.1-r0"  # type: ignore[attr-defined]
    classified._task = "do_compile"  # type: ignore[attr-defined]
    classified.type = "bakar-cache-backend"  # type: ignore[attr-defined]
    classified._localdata = "sccache"  # type: ignore[attr-defined]

    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(
        json.dumps({"class": "bb.build.TaskStarted", "vars": _encode_event(started)})
        + "\n"
        + json.dumps({"class": "bb.event.MetadataEvent", "vars": _encode_event(classified)})
        + "\n",
        encoding="utf-8",
    )

    artifact = eventlog.normalize(log)
    task = next(t for t in artifact["tasks"] if t["recipe"] == "busybox-1.36.1-r0")
    assert task["cache_backend"] == "sccache"


@pytest.mark.unit
def test_task_without_metadata_event_has_null_cache_backend(tmp_path: Path) -> None:
    """A task row with no classifying MetadataEvent carries cache_backend as
    ``None`` and present as a key - distinguishing "unclassified" from any
    classified value, never simply absent from the row."""
    started = _StubEvent()
    started._package = "zlib-1.3-r0"  # type: ignore[attr-defined]
    started._task = "do_fetch"  # type: ignore[attr-defined]
    started.time = 50.0  # type: ignore[attr-defined]

    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(
        json.dumps({"class": "bb.build.TaskStarted", "vars": _encode_event(started)}) + "\n",
        encoding="utf-8",
    )

    artifact = eventlog.normalize(log)
    task = next(t for t in artifact["tasks"] if t["recipe"] == "zlib-1.3-r0")
    assert "cache_backend" in task
    assert task["cache_backend"] is None


@pytest.mark.unit
def test_non_utf8_log_does_not_raise(tmp_path: Path) -> None:
    """A non-UTF-8 byte in the log (aborted/concurrent build) must not raise
    UnicodeDecodeError out of normalize - it degrades to skipping that line."""
    raw = tmp_path / "bitbake_eventlog.json"
    good = FIXTURE.read_bytes().splitlines()[0]
    raw.write_bytes(good + b"\n" + b"\xff\xfe not utf-8 \x80\n")

    artifact = eventlog.normalize(raw)

    assert set(artifact) == {"schema_version", "build", "tasks", "setscene", "failures", "psi", "disk"}


def _task_event(class_name: str, recipe: str, task: str, *, started: float | None = None) -> str:
    """Build one JSONL event line for ``class_name`` with recipe/task/time fields."""
    ev = _StubEvent()
    ev._package = recipe  # type: ignore[attr-defined]
    ev._task = task  # type: ignore[attr-defined]
    if started is not None:
        ev.time = started  # type: ignore[attr-defined]
    return json.dumps({"class": class_name, "vars": _encode_event(ev)})


def _write_eventlog(run_dir: Path, lines: list[str]) -> None:
    (run_dir / "bitbake_eventlog.json").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.unit
def test_running_tasks_reports_running(tmp_path: Path) -> None:
    """A started-but-not-completed task is reported; a completed one is excluded."""
    _write_eventlog(
        tmp_path,
        [
            _task_event("bb.build.TaskStarted", "busybox-1.36.1-r0", "do_compile", started=100.0),
            _task_event("bb.build.TaskStarted", "zlib-1.3-r0", "do_fetch", started=50.0),
            _task_event("bb.build.TaskSucceeded", "zlib-1.3-r0", "do_fetch"),
        ],
    )

    running = eventlog.running_tasks(tmp_path)

    assert running == [eventlog.RunningTask(recipe="busybox-1.36.1-r0", task="do_compile", started_epoch=100.0)]


@pytest.mark.unit
def test_running_tasks_excludes_completed(tmp_path: Path) -> None:
    """The committed fixture has no running task (busybox is succeeded)."""
    _write_eventlog(tmp_path, FIXTURE.read_text(encoding="utf-8").splitlines())

    assert eventlog.running_tasks(tmp_path) == []


@pytest.mark.unit
def test_running_tasks_absent_log_returns_empty(tmp_path: Path) -> None:
    """A run dir with no event log yields ``[]`` without raising."""
    assert eventlog.running_tasks(tmp_path) == []


@pytest.mark.unit
def test_running_tasks_malformed_log_returns_empty(tmp_path: Path) -> None:
    """A truncated/non-UTF-8 event log yields ``[]`` without raising."""
    (tmp_path / "bitbake_eventlog.json").write_bytes(b"\xff\xfe not json {\x80 truncated")

    assert eventlog.running_tasks(tmp_path) == []


@pytest.mark.unit
def test_normalize_psi_disk_absent_when_no_records(tmp_path: Path) -> None:
    """No PSIEvent/MonitorDiskEvent/DiskUsageSample/DiskFull records in the raw
    log yields empty psi/disk sections, schema_version 4, and no exception."""
    _write_eventlog(
        tmp_path,
        [_task_event("bb.build.TaskStarted", "busybox-1.36.1-r0", "do_compile", started=100.0)],
    )

    artifact = eventlog.normalize(tmp_path / "bitbake_eventlog.json")

    assert artifact["schema_version"] == 4
    assert artifact["psi"] == {"samples": []}
    assert artifact["disk"] == {"samples": [], "full_events": []}


@pytest.mark.unit
def test_normalize_captures_psi_event(tmp_path: Path) -> None:
    """A PSIEvent record decodes into psi.samples with cpu/io/memory fields."""
    ev = _StubEvent()
    ev.time = 42.0  # type: ignore[attr-defined]
    ev.cpu = 12.5  # type: ignore[attr-defined]
    ev.io = 60.0  # type: ignore[attr-defined]
    ev.memory = 3.0  # type: ignore[attr-defined]

    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(
        json.dumps({"class": "bb.event.PSIEvent", "vars": _encode_event(ev)}) + "\n",
        encoding="utf-8",
    )

    artifact = eventlog.normalize(log)

    assert artifact["schema_version"] == 4
    assert artifact["psi"]["samples"] == [{"time": 42.0, "cpu": 12.5, "io": 60.0, "memory": 3.0}]
    assert artifact["disk"] == {"samples": [], "full_events": []}


@pytest.mark.unit
def test_normalize_captures_disk_usage_and_full_events(tmp_path: Path) -> None:
    """MonitorDiskEvent.disk_usage (a dict of DiskUsageSample) folds into
    disk.samples; DiskFull's dev/type/free/mountpoint fields are tracked
    separately in disk.full_events - matching real bb.event.MonitorDiskEvent
    (self.disk_usage) and bb.event.DiskFull (self._dev/_type/_free/_mountpoint)
    from bitbake/lib/bb/event.py, not a flat path/used/free/total/message
    guess."""
    usage_event = _StubEvent()
    usage_event.disk_usage = {  # type: ignore[attr-defined]
        "/work/build": _StubDiskUsageSample(available_bytes=45, free_bytes=50, total_bytes=150)
    }

    full = _StubEvent()
    full._dev = "/dev/sda1"  # type: ignore[attr-defined]
    full._type = "ext4"  # type: ignore[attr-defined]
    full._free = 1024  # type: ignore[attr-defined]
    full._mountpoint = "/work/build"  # type: ignore[attr-defined]

    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(
        "\n".join(
            [
                json.dumps({"class": "bb.event.MonitorDiskEvent", "vars": _encode_event(usage_event)}),
                json.dumps({"class": "bb.event.DiskFull", "vars": _encode_event(full)}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    artifact = eventlog.normalize(log)

    assert artifact["schema_version"] == 4
    assert artifact["disk"]["samples"] == [
        {"path": "/work/build", "used": 100, "free": 50, "total": 150},
    ]
    assert artifact["disk"]["full_events"] == [
        {"dev": "/dev/sda1", "type": "ext4", "free_bytes": 1024, "mountpoint": "/work/build"}
    ]
    assert artifact["psi"] == {"samples": []}
