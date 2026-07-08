"""Tests for :func:`bakar.eventlog.tail_events`, the streaming reader.

``tail_events`` follows a growing bitbake event-log JSONL file and yields each
decoded ``(class_name, event)`` pair as it lands. Because the generator blocks
on EOF waiting for new content, these tests drive it from a worker thread and
collect yielded pairs into a list, then set the stop event to tear it down.
Every test that exercises the blocking path carries a bounded
``@pytest.mark.timeout`` so a hang fails the test rather than wedging the run.

Temp logs are built from the valid base64 lines committed in
``tests/fixtures/bitbake_eventlog.json``; the deliberately-truncated trailing
line is reused only by the partial-line tolerance test.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bakar.eventlog import tail_events

FIXTURE = Path(__file__).parent / "fixtures" / "bitbake_eventlog.json"

# Valid, newline-terminated JSONL lines copied from the fixture. Line indices
# (1-based in the file): the TaskStarted, TaskSucceeded, and runQueueTaskStarted
# lines decode cleanly and are reused to build temp logs.
_FIXTURE_LINES = [ln for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]
TASK_STARTED_LINE = next(ln for ln in _FIXTURE_LINES if "bb.build.TaskStarted" in ln)
TASK_SUCCEEDED_LINE = next(ln for ln in _FIXTURE_LINES if "bb.build.TaskSucceeded" in ln)
RUNQUEUE_LINE = next(ln for ln in _FIXTURE_LINES if "runQueueTaskStarted" in ln)


def _drain_in_thread(path: Path, stop: threading.Event, collected: list[tuple[str, object]]) -> threading.Thread:
    """Run ``tail_events`` in a daemon thread, appending pairs to ``collected``."""

    def _run() -> None:
        # Append-as-streamed so the main thread sees pairs incrementally; this
        # is not a list copy (PERF402), the source is a blocking generator.
        for pair in tail_events(path, stop):
            collected.append(pair)  # noqa: PERF402

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_incremental_yield_after_append(tmp_path: Path) -> None:
    """A line appended after the reader starts is yielded incrementally."""
    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(TASK_STARTED_LINE + "\n", encoding="utf-8")

    stop = threading.Event()
    collected: list[tuple[str, object]] = []
    thread = _drain_in_thread(log, stop, collected)
    try:
        assert _wait_for(lambda: len(collected) >= 1), "pre-existing line not yielded"
        assert collected[0][0] == "bb.build.TaskStarted"

        with log.open("a", encoding="utf-8") as fh:
            fh.write(TASK_SUCCEEDED_LINE + "\n")

        assert _wait_for(lambda: len(collected) >= 2), "appended line not yielded"
        assert collected[1][0] == "bb.build.TaskSucceeded"
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_preexisting_events_yielded_before_appended(tmp_path: Path) -> None:
    """Two pre-written lines are both yielded before an appended one."""
    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(TASK_STARTED_LINE + "\n" + TASK_SUCCEEDED_LINE + "\n", encoding="utf-8")

    stop = threading.Event()
    collected: list[tuple[str, object]] = []
    thread = _drain_in_thread(log, stop, collected)
    try:
        assert _wait_for(lambda: len(collected) >= 2), "pre-existing lines not yielded"
        assert [c[0] for c in collected[:2]] == [
            "bb.build.TaskStarted",
            "bb.build.TaskSucceeded",
        ]

        with log.open("a", encoding="utf-8") as fh:
            fh.write(RUNQUEUE_LINE + "\n")

        assert _wait_for(lambda: len(collected) >= 3), "appended line not yielded"
        assert collected[2][0] == "bb.runqueue.runQueueTaskStarted"
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_stop_while_waiting_at_eof_terminates(tmp_path: Path) -> None:
    """Setting the stop event while blocked at EOF terminates the generator."""
    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(TASK_STARTED_LINE + "\n", encoding="utf-8")

    stop = threading.Event()
    collected: list[tuple[str, object]] = []
    thread = _drain_in_thread(log, stop, collected)
    try:
        # Let it consume the one line and settle into the EOF wait loop.
        assert _wait_for(lambda: len(collected) >= 1)
        time.sleep(0.3)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive(), "generator hung at EOF after stop was set"


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_stop_before_file_creation_terminates(tmp_path: Path) -> None:
    """A never-created file plus a set stop terminates with zero yields."""
    log = tmp_path / "never_created.json"
    assert not log.exists()

    stop = threading.Event()
    collected: list[tuple[str, object]] = []
    thread = _drain_in_thread(log, stop, collected)
    try:
        # Give the wait-for-file poll a chance to spin before stopping.
        time.sleep(0.3)
        stop.set()
        thread.join(timeout=5)
    finally:
        stop.set()
    assert not thread.is_alive(), "generator hung waiting for a file that never appeared"
    assert collected == [], "yielded events though the file never existed"


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_partial_trailing_line_not_yielded_until_complete(tmp_path: Path) -> None:
    """An incomplete final line is withheld until it gains a trailing newline."""
    log = tmp_path / "bitbake_eventlog.json"
    # One complete line, then a partial line with NO trailing newline.
    head, _, tail = RUNQUEUE_LINE.partition('"vars"')
    partial = head + '"vars"'  # truncated mid-record, no newline
    log.write_text(TASK_STARTED_LINE + "\n" + partial, encoding="utf-8")

    stop = threading.Event()
    collected: list[tuple[str, object]] = []
    thread = _drain_in_thread(log, stop, collected)
    try:
        assert _wait_for(lambda: len(collected) >= 1), "complete line not yielded"
        assert collected[0][0] == "bb.build.TaskStarted"

        # The partial line must not be yielded while it lacks a newline.
        time.sleep(0.4)
        assert len(collected) == 1, "partial trailing line was yielded prematurely"

        # Complete the line: append the remainder plus a trailing newline.
        with log.open("a", encoding="utf-8") as fh:
            fh.write(tail + "\n")

        assert _wait_for(lambda: len(collected) >= 2), "completed line not yielded"
        assert collected[1][0] == "bb.runqueue.runQueueTaskStarted"
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
