"""Host-side reader for bitbake's persisted event log.

bitbake writes a structured event log when ``BB_DEFAULT_EVENTLOG`` is set
(``cooker.py:304`` honors it literally). The log is JSON Lines: most lines
are ``{"class": "bb.build.TaskFailed", "vars": "<base64(pickle(event))>"}``
where the payload is a base64-encoded Python pickle of the bb event object;
one line is ``{"allvariables": {...}}`` (the variable dump).

bakar runs on the host and does NOT bundle bitbake, so the payload cannot be
unpickled the normal way - ``bb.*`` is not importable and version drift between
the writing bitbake and any host ``bb`` would break a naive ``pickle.loads``.

This module decodes each ``vars`` payload with a restricted
:class:`pickle.Unpickler` whose :meth:`find_class` returns an inert stub class.
The stub accepts arbitrary constructor args and arbitrary attribute assignment,
capturing the event's instance ``__dict__`` without ever importing ``bb``.
Fields are then extracted by name with ``getattr`` fallbacks, so unknown event
classes degrade to "skipped" and a renamed field degrades to ``None`` rather
than raising. The ``bb`` package is never imported; no bitbake dependency
is added.

:func:`normalize` reads a raw log path and returns the normalized artifact dict
matching the ``bitbake-events.json`` schema (the downstream contract):

    {schema_version, build, tasks, setscene, failures}

Absent optional fields are emitted as ``null``/empty, never dropped.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import pickle
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading
    from pathlib import Path

# Bumped when the artifact shape changes so downstream consumers
# (build-insights, triage) can detect format drift.
SCHEMA_VERSION = 1

# bitbake event class names (the JSON line's ``class`` field) we recognize.
# Each decoded event is classified by this string, NOT by isinstance - the
# stub is inert and carries no real type identity.
_BUILD_STARTED = "bb.event.BuildStarted"
_BUILD_COMPLETED = "bb.event.BuildCompleted"
_TASK_STARTED = "bb.build.TaskStarted"
_TASK_SUCCEEDED = "bb.build.TaskSucceeded"
_TASK_FAILED = "bb.build.TaskFailed"
_TASK_FAILED_SILENT = "bb.build.TaskFailedSilent"
_RUNQUEUE_TASK_STARTED = "bb.runqueue.runQueueTaskStarted"

_TASK_CLASSES = frozenset(
    {
        _TASK_STARTED,
        _TASK_SUCCEEDED,
        _TASK_FAILED,
        _TASK_FAILED_SILENT,
    }
)

_RECOGNIZED_CLASSES = _TASK_CLASSES | {
    _BUILD_STARTED,
    _BUILD_COMPLETED,
    _RUNQUEUE_TASK_STARTED,
}


class _EventStub:
    """Inert stand-in for any pickled bitbake event class.

    Accepts arbitrary ``__init__`` args and arbitrary attribute assignment so
    that whatever pickle restores - via ``__init__``, ``__setstate__``, or a
    direct ``instance.__dict__`` update - lands as plain attributes we can read
    with ``getattr``. No real ``bb`` class is ever imported or constructed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Some events are reconstructed via __init__ with positional args we
        # cannot name; stash them so nothing is lost, then absorb kwargs.
        if args:
            object.__setattr__(self, "_stub_args", args)
        for key, value in kwargs.items():
            object.__setattr__(self, key, value)

    def __setstate__(self, state: Any) -> None:
        # Pickle calls this (when present) instead of updating __dict__ directly.
        if isinstance(state, dict):
            for key, value in state.items():
                object.__setattr__(self, key, value)
        else:
            object.__setattr__(self, "_stub_state", state)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        # Any attribute the event did not define reads as None instead of
        # raising AttributeError, so field extraction never crashes.
        return None


class _StubUnpickler(pickle.Unpickler):
    """Unpickler that resolves every class to :class:`_EventStub`."""

    def find_class(self, module: str, name: str) -> type:
        return _EventStub


def _decode_event(payload: str) -> _EventStub | None:
    """Base64-decode and unpickle one ``vars`` payload into a stub.

    Returns ``None`` (rather than raising) on any decode/unpickle failure so a
    single malformed event never aborts the whole log.
    """
    try:
        raw = base64.b64decode(payload)
    except binascii.Error, ValueError:
        return None
    try:
        obj = _StubUnpickler(io.BytesIO(raw)).load()
    except Exception:
        # Any pickle error (truncated stream, opcode the stub cannot satisfy,
        # version drift) degrades to "skip this event", never a crash.
        return None
    return obj if isinstance(obj, _EventStub) else None


def _first(event: _EventStub, *names: str) -> Any:
    """Return the first non-None attribute among ``names``, else None."""
    for name in names:
        value = getattr(event, name, None)
        if value is not None:
            return value
    return None


def _iter_events(raw_path: Path):
    """Yield ``(class_name, event_stub)`` for each recognized event line.

    Skips the ``{"allvariables": ...}`` line, lines whose class is
    unrecognized, lines that fail to decode, and a truncated/malformed
    trailing line - none of these raise.
    """
    # errors="replace": a non-UTF-8 or truncated-mid-multibyte log (aborted or
    # concurrent build) must not raise UnicodeDecodeError during line iteration,
    # which would escape the per-line json guard below and crash the caller.
    with raw_path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError, ValueError:
                # Truncated/malformed line (e.g. a killed build's final line).
                continue
            if not isinstance(record, dict):
                continue
            if "allvariables" in record:
                # The variable dump is not an event; no consumer yet.
                continue
            class_name = record.get("class")
            payload = record.get("vars")
            if not isinstance(class_name, str) or not isinstance(payload, str):
                continue
            if class_name not in _RECOGNIZED_CLASSES:
                continue
            event = _decode_event(payload)
            if event is None:
                continue
            yield class_name, event


def _decode_line(line: str) -> tuple[str, _EventStub] | None:
    """Decode one stripped JSONL line into ``(class_name, event)`` or ``None``.

    Applies the same per-line guards as :func:`_iter_events` (skip blanks, bad
    JSON, non-dict records, the ``allvariables`` dump, and records missing a
    str ``class``/``vars``), but does NOT filter on ``_RECOGNIZED_CLASSES`` -
    the live feed needs ``bb.event.ParseProgress``/``bb.event.CacheLoadProgress``
    too. Returns ``None`` for any line that should be skipped.
    """
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError, ValueError:
        return None
    if not isinstance(record, dict) or "allvariables" in record:
        return None
    class_name = record.get("class")
    payload = record.get("vars")
    if not isinstance(class_name, str) or not isinstance(payload, str):
        return None
    event = _decode_event(payload)
    if event is None:
        return None
    return class_name, event


def tail_events(path: Path, stop_event: threading.Event):
    """Follow a growing bitbake event log and yield decoded events as they land.

    A streaming counterpart to :func:`_iter_events` for the live build display.
    It waits (short poll) for ``path`` to appear when absent at start, yields
    every pre-existing event, then keeps following the file: on EOF it sleeps
    briefly and re-checks, yielding each newly-appended line. Unlike the batch
    path it yields *every* line that decodes regardless of class (the live feed
    needs ``ParseProgress``/``CacheLoadProgress``, which are not in
    ``_RECOGNIZED_CLASSES``); only the ``allvariables`` dump and undecodable
    lines are skipped.

    Only newline-terminated lines are processed: a partial trailing line (the
    writer mid-append) is buffered and re-read once complete, mirroring
    ``normalize``'s truncation tolerance. The reader stops once ``stop_event``
    is set, draining any remaining complete lines first; if the stop fires
    before the file is ever created, it returns without yielding. No ``bb``
    module is imported.
    """
    while not path.is_file():
        if stop_event.wait(0.2):
            return

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        while True:
            offset = fh.tell()
            chunk = fh.readline()
            if chunk.endswith("\n"):
                decoded = _decode_line(chunk.strip())
                if decoded is not None:
                    yield decoded
                continue
            # Either EOF or a partial trailing line still being written.
            # Rewind so the incomplete line is re-read once it completes.
            fh.seek(offset)
            if stop_event.is_set():
                return
            stop_event.wait(0.2)


def _task_key(event: _EventStub) -> tuple[Any, Any]:
    recipe = _first(event, "_package")
    task = _first(event, "_task", "taskname")
    return recipe, task


def normalize(raw_path: Path) -> dict[str, Any]:
    """Read a raw bitbake event log and return the normalized artifact.

    The returned dict always has exactly these top-level keys::

        {schema_version, build, tasks, setscene, failures}

    ``failures[]`` entries carry ``recipe`` (from ``_package``), ``task``
    (from ``_task``/``taskname``), ``logfile``, and ``errprinted``.
    ``TaskFailedSilent`` (setscene) is recorded in ``tasks`` with outcome
    ``failed_silent`` but excluded from top-level ``failures`` (mirrors
    bitbake's ``BBUIHelper``). Setscene stats come from the first
    ``runQueueTaskStarted.stats`` seen. Absent optional fields are emitted as
    ``null``/empty, never dropped.

    A missing, empty, or wholly-malformed log yields an artifact with empty
    collections and an ``unknown`` build outcome rather than raising.
    """
    build: dict[str, Any] = {
        "started": None,
        "completed": None,
        "outcome": "unknown",
        "preset": None,
        "release": None,
        "run_id": None,
    }
    # Keyed by (recipe, task) so a task's start/end events merge into one row.
    tasks: dict[tuple[Any, Any], dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    setscene: dict[str, Any] = {
        "covered": 0,
        "notcovered": 0,
        "total": 0,
        "per_recipe": [],
    }
    setscene_seen = False
    saw_failure = False

    if raw_path is None or not raw_path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "build": build,
            "tasks": [],
            "setscene": setscene,
            "failures": failures,
        }

    def _task_row(event: _EventStub) -> dict[str, Any]:
        key = _task_key(event)
        row = tasks.get(key)
        if row is None:
            row = {
                "recipe": _first(event, "_package"),
                "task": _first(event, "_task", "taskname"),
                "outcome": None,
                "started": None,
                "completed": None,
                "pid": None,
                "logfile": None,
            }
            tasks[key] = row
        return row

    for class_name, event in _iter_events(raw_path):
        if class_name == _BUILD_STARTED:
            build["started"] = _first(event, "time", "timestamp")
        elif class_name == _BUILD_COMPLETED:
            build["completed"] = _first(event, "time", "timestamp")
            if build["outcome"] == "unknown":
                build["outcome"] = "failed" if saw_failure else "success"
        elif class_name == _TASK_STARTED:
            row = _task_row(event)
            row["started"] = _first(event, "time", "timestamp")
            row["pid"] = _first(event, "pid")
            if row["logfile"] is None:
                row["logfile"] = _first(event, "logfile")
        elif class_name == _TASK_SUCCEEDED:
            row = _task_row(event)
            row["outcome"] = "succeeded"
            row["completed"] = _first(event, "time", "timestamp")
        elif class_name == _TASK_FAILED:
            row = _task_row(event)
            row["outcome"] = "failed"
            row["completed"] = _first(event, "time", "timestamp")
            logfile = _first(event, "logfile")
            if logfile is not None:
                row["logfile"] = logfile
            saw_failure = True
            failures.append(
                {
                    "recipe": _first(event, "_package"),
                    "task": _first(event, "_task", "taskname"),
                    "logfile": logfile,
                    "errprinted": _first(event, "errprinted"),
                }
            )
        elif class_name == _TASK_FAILED_SILENT:
            # setscene failure: tracked in tasks, NEVER in top-level failures.
            row = _task_row(event)
            row["outcome"] = "failed_silent"
            row["completed"] = _first(event, "time", "timestamp")
            logfile = _first(event, "logfile")
            if logfile is not None:
                row["logfile"] = logfile
        elif class_name == _RUNQUEUE_TASK_STARTED and not setscene_seen:
            stats = getattr(event, "stats", None)
            covered = _stat(stats, "setscene_covered")
            notcovered = _stat(stats, "setscene_notcovered")
            total = _stat(stats, "setscene_total")
            if covered is not None or notcovered is not None or total is not None:
                setscene["covered"] = covered or 0
                setscene["notcovered"] = notcovered or 0
                setscene["total"] = total or 0
                setscene_seen = True

    if build["outcome"] == "unknown" and saw_failure:
        build["outcome"] = "failed"

    return {
        "schema_version": SCHEMA_VERSION,
        "build": build,
        "tasks": list(tasks.values()),
        "setscene": setscene,
        "failures": failures,
    }


def _stat(stats: Any, name: str) -> Any:
    """Read ``name`` from a stats object that may be a stub or a dict."""
    if stats is None:
        return None
    if isinstance(stats, dict):
        return stats.get(name)
    return getattr(stats, name, None)
