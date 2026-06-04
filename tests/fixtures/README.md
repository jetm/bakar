# Test fixtures

## `bitbake_eventlog.json`

A hermetic stand-in for the event log bitbake writes when `BB_DEFAULT_EVENTLOG`
is set (`cooker.py:EventWriter.send`). It is **JSON Lines** (one JSON object per
line, not a JSON array). Each line is either an event record or the one-shot
variable dump:

- Event line: `{"class": "bb.build.TaskFailed", "vars": "<base64(pickle(event))>"}`
  where the payload is a base64-encoded Python pickle of the event object.
- Variable dump: `{"allvariables": {...}}` (written once via `write_variables`).

The fixture was synthesized **without bitbake installed** so the test suite stays
hermetic. The reader (`src/bakar/eventlog.py`) decodes these payloads with a
restricted unpickler whose `find_class` returns an inert stub, so neither this
fixture nor the reader ever imports `bb`.

### How it was generated

For each event the generator:

1. Creates a fresh stub class with `type(qualname, (), {})`.
2. Forces `cls.__module__` / `cls.__qualname__` to the target identity
   (e.g. `bb.build` / `TaskFailed`). Pickle records this `(module, qualname)`
   pair in the bytestream - that is the only identity the reader needs.
3. Registers a fake `bb`, `bb.build`, ... module tree in `sys.modules` with the
   stub bound under its qualname. This is required only so the pickle protocol's
   class-reachability check passes at dump time; it leaves no trace in the
   committed bytes.
4. Instantiates via `cls.__new__(cls)` (bypassing `__init__`, matching how
   bitbake events restore from `__dict__`), sets the instance attributes
   (`_package`, `_task`, `taskname`, `logfile`, `errprinted` for `TaskFailed`;
   `stats` with `setscene_*` counters for `runQueueTaskStarted`), pickles, and
   base64-encodes.

### Contents (in order)

| Line | Class | Notes |
|------|-------|-------|
| 1 | `bb.event.BuildStarted` | build start |
| 2 | `bb.build.TaskStarted` | busybox `do_compile` start |
| 3 | `bb.build.TaskSucceeded` | busybox `do_compile` success |
| 4 | `bb.build.TaskFailed` | linux-imx `do_compile`; `logfile` under `/work/...`, `_package`/`_task`/`errprinted` set |
| 5 | `bb.build.TaskFailedSilent` | setscene failure; tracked but not a top-level failure |
| 6 | `bb.runqueue.runQueueTaskStarted` | carries `stats` with `setscene_covered`/`setscene_notcovered`/`setscene_total` |
| 7 | `bb.event.SomeFutureEventThatBakarDoesNotKnow` | unrecognized class - reader must skip, not crash |
| 8 | `{"allvariables": {...}}` | the variable dump line |
| 9 | (truncated) | deliberately incomplete JSON object, **no trailing newline** - simulates a build killed mid-write; the reader must skip it |

To regenerate, re-run the synthesis steps above against the same event list.
