"""Tests for :class:`bakar.eventlog.IncrementalEventLog`.

The property test is the proof the incremental view cannot drift from the
batch one: it cuts a recorded log at random byte offsets, appends it chunk by
chunk, and after every chunk compares the reader against ``normalize`` and
``running_tasks`` run over the same bytes truncated to the last newline.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar import eventlog
from bakar.eventlog import IncrementalEventLog
from tests.test_eventlog import _task_event

FIXTURE = Path(__file__).parent / "fixtures" / "bitbake_eventlog.json"


def _log_bytes() -> bytes:
    """Fixture lines plus synthetic task lines, junk and a non-UTF-8 line."""
    lines = [ln.encode() for ln in FIXTURE.read_text(encoding="utf-8").splitlines()[:-1] if ln.strip()]
    for i in range(6):
        recipe = f"pkg{i}-1.0-r0"
        lines.append(_task_event("bb.build.TaskStarted", recipe, "do_compile", started=100.0 + i).encode())
        if i % 2 == 0:
            lines.append(_task_event("bb.build.TaskSucceeded", recipe, "do_compile").encode())
        elif i == 3:
            lines.append(_task_event("bb.build.TaskFailed", recipe, "do_compile").encode())
    lines.append(b"not json at all")
    lines.append(b'{"class": "bb.build.TaskStarted", "vars": "\xff\xfe"}')
    return b"\n".join(lines) + b"\n"


LOG = _log_bytes()


def _expected(tmp_path: Path, data: bytes) -> tuple[dict, list]:
    """Batch view over ``data`` truncated to its last newline."""
    cut = data[: data.rfind(b"\n") + 1]
    ref = tmp_path / "ref" / "bitbake_eventlog.json"
    ref.parent.mkdir(exist_ok=True)
    ref.write_bytes(cut)
    return eventlog.normalize(ref), eventlog.running_tasks(ref.parent)


@pytest.mark.unit
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cuts=st.lists(st.integers(min_value=0, max_value=len(LOG)), max_size=12))
def test_incremental_equals_batch_after_every_chunk(tmp_path: Path, cuts: list[int]) -> None:
    path = tmp_path / "bitbake_eventlog.json"
    path.write_bytes(b"")
    reader = IncrementalEventLog(path)
    points = sorted({*cuts, len(LOG)})
    prev = 0
    for point in points:
        with path.open("ab") as fh:
            fh.write(LOG[prev:point])
        prev = point
        before = reader.offset
        reader.poll()
        assert reader.offset >= before
        assert reader.offset == LOG[:point].rfind(b"\n") + 1
        want_norm, want_running = _expected(tmp_path, LOG[:point])
        assert reader.normalized() == want_norm
        assert reader.running_tasks() == want_running


@pytest.mark.unit
def test_poll_does_not_reread_consumed_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "log"
    path.write_bytes(LOG)
    reader = IncrementalEventLog(path)
    reader.poll()
    consumed = reader.offset
    assert consumed == len(LOG)

    seen: list[int] = []
    real_decode = eventlog._recognized_line
    monkeypatch.setattr(eventlog, "_recognized_line", lambda *a, **k: seen.append(1) or real_decode(*a, **k))
    reader.poll()
    assert seen == []
    assert reader.offset == consumed


@pytest.mark.unit
def test_partial_trailing_line_waits(tmp_path: Path) -> None:
    path = tmp_path / "log"
    line = _task_event("bb.build.TaskStarted", "a-1-r0", "do_x", started=1.0).encode()
    path.write_bytes(line[:10])
    reader = IncrementalEventLog(path)
    reader.poll()
    assert reader.offset == 0
    assert reader.running_tasks() == []
    with path.open("ab") as fh:
        fh.write(line[10:] + b"\n")
    reader.poll()
    assert [t.recipe for t in reader.running_tasks()] == ["a-1-r0"]


@pytest.mark.unit
def test_shrink_restarts_from_zero(tmp_path: Path) -> None:
    path = tmp_path / "log"
    path.write_bytes(LOG)
    reader = IncrementalEventLog(path)
    reader.poll()
    short = _task_event("bb.build.TaskStarted", "only-1-r0", "do_x", started=1.0).encode() + b"\n"
    assert len(short) < len(LOG)
    with path.open("r+b") as fh:
        fh.truncate(0)
        fh.write(short)
    reader.poll()
    assert reader.offset == len(short)
    assert [t.recipe for t in reader.running_tasks()] == ["only-1-r0"]
    assert reader.normalized() == _expected(tmp_path, short)[0]


@pytest.mark.unit
def test_replaced_file_restarts_even_when_larger(tmp_path: Path) -> None:
    path = tmp_path / "log"
    first = _task_event("bb.build.TaskStarted", "old-1-r0", "do_x", started=1.0).encode() + b"\n"
    path.write_bytes(first)
    reader = IncrementalEventLog(path)
    reader.poll()
    old_inode = os.stat(path).st_ino

    replacement = tmp_path / "new"
    replacement.write_bytes(LOG)
    # Hold the old inode alive so the replacement cannot reuse its number.
    keep = tmp_path / "keep"
    os.link(path, keep)
    os.replace(replacement, path)
    assert os.stat(path).st_ino != old_inode

    reader.poll()
    assert reader.offset == len(LOG)
    assert reader.normalized() == _expected(tmp_path, LOG)[0]
    assert "old-1-r0" not in [t.recipe for t in reader.running_tasks()]


@pytest.mark.unit
def test_missing_file_is_empty_then_recovers(tmp_path: Path) -> None:
    path = tmp_path / "log"
    reader = IncrementalEventLog(path)
    reader.poll()
    assert reader.running_tasks() == []
    assert reader.normalized() == eventlog.normalize(path)
    path.write_bytes(LOG)
    reader.poll()
    assert reader.normalized() == _expected(tmp_path, LOG)[0]


@pytest.mark.unit
def test_poison_line_does_not_replay_earlier_lines(tmp_path: Path) -> None:
    path = tmp_path / "log"
    good = (
        _task_event("bb.build.TaskStarted", "a-1-r0", "do_x", started=1.0).encode()
        + b"\n"
        + _task_event("bb.build.TaskFailed", "a-1-r0", "do_x").encode()
        + b"\n"
    )
    path.write_bytes(good + b"[" * 200000 + b"\n")
    reader = IncrementalEventLog(path)
    counts = []
    for _ in range(3):
        reader.poll()
        counts.append(len(reader.normalized()["failures"]))
    assert counts == [1, 1, 1]
    assert reader.offset == path.stat().st_size


@pytest.mark.unit
def test_large_log_matches_one_shot_across_partial_polls(tmp_path: Path) -> None:
    path = tmp_path / "log"
    big = LOG * 1000
    assert len(big) > 4 * 1024 * 1024
    reader = IncrementalEventLog(path)
    cuts = [0, len(big) // 3 + 7, 2 * len(big) // 3 + 3, len(big)]
    for lo, hi in itertools.pairwise(cuts):
        with path.open("ab") as fh:
            fh.write(big[lo:hi])
        reader.poll()
    expected = _expected(tmp_path, big)
    assert reader.normalized() == expected[0]
    assert reader.running_tasks() == expected[1]
    assert reader.offset == len(big)


@pytest.mark.unit
def test_incomplete_trailing_line_waits_for_newline(tmp_path: Path) -> None:
    path = tmp_path / "log"
    line = _task_event("bb.build.TaskStarted", "a-1-r0", "do_x", started=1.0).encode()
    path.write_bytes(line + b"\n" + line[:10])
    reader = IncrementalEventLog(path)
    reader.poll()
    assert reader.offset == len(line) + 1
    with path.open("ab") as fh:
        fh.write(line[10:] + b"\n")
    reader.poll()
    assert reader.offset == 2 * (len(line) + 1)
