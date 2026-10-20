# bakar status

Report one lifecycle state for a build run: `starting`, `running`,
`succeeded`, `failed` or `unknown`. Built for scripts and agents that launched
a build with [`bakar build --detach`](build.md) and need to know where it
stands without parsing logs.

## Synopsis

```text
bakar status [HANDLE] [OPTIONS]
```

`HANDLE` is a run id or a detached unit name. With no handle, `status`
reports the newest run of the current workspace.

## Options

| Flag | Description |
|------|-------------|
| `--json` | Emit one JSON object on stdout instead of a one-line summary |
| `--detail` | Add the [detail snapshot](#detail-snapshot): running and failed tasks, the last log line, disk, memory and pressure |
| `-w`, `--workspace PATH` | Workspace to search for a foreground run (default: resolved from the current directory) |
| `--on HOST` | Query the run on `HOST` over ssh instead of locally |

## Handle resolution

| Handle | Resolves to |
|--------|-------------|
| A unit name (`bakar-detach-YYYYMMDD-HHMMSS-xxxxxx` or `bakar-dispatch-...`) | The detached run behind that unit. A unit with no registered run yet is still real while systemd knows it or a sentinel exists |
| A run id registered by a detached run | The same detached run, found by its run id |
| Any other run id | A foreground run, found through the workspace the same way [`bakar triage`](triage.md) finds it |
| (omitted) | The newest run directory of the current workspace |

Detached runs resolve from any directory, because their records live in the
runtime directory rather than in a workspace. Foreground runs need a workspace,
so run `status` from inside it or pass `--workspace`.

## States

| State | Meaning |
|-------|---------|
| `starting` | The detached unit is active but has not registered a run id yet |
| `running` | The unit is active, or the run directory's build process is alive |
| `succeeded` | A recorded exit status of 0 |
| `failed` | A recorded exit status other than 0 |
| `unknown` | No recorded exit status and no sign of life: the process vanished without saying how it ended |

Only a recorded exit status decides `succeeded` or `failed`. For a run id,
`status` checks, in order:

1. While the run's detached unit is active and has no sentinel: `running`.
2. The run's own terminal event (`run_end` or `run_error`) in `events.jsonl`.
3. The detached unit's sentinel file (its exit status), only when the run
   recorded no terminal event.
4. Liveness, which separates `running` from `unknown` and never produces
   `succeeded` or `failed`. A run is live while its build process is alive
   (`build.pid` names a process group whose command line is a kas process) or
   while its front-end process, the pid after the last hyphen of the run id, is
   a live `bakar` process.

A unit name reads the unit's sentinel and never a per-run event. An inactive
unit with no sentinel (stopped with `systemctl stop`, killed by the OOM killer,
or its session ended without linger) reports `unknown`.

While a detached unit is active, every run it registered reports `running`.
Afterwards, for a multi-release preset run, each run reports its own recorded
exit status and the unit's sentinel only when the run recorded none.

A run that died from SIGKILL, a reboot or an out-of-memory kill leaves no exit
status behind, so it reports `unknown` rather than a guessed failure.

When `XDG_RUNTIME_DIR` is unset, relative or unsafe, a unit name that nothing
else resolves exits 2 and stderr names the reason.

## Output

Plain text is one line:

```text
20260701-090000-48213 running tasks=1204/7312 running=24
20260701-090000-48213 failed exit=1 tasks=1210/7312 running=? failed=openssl:do_compile
```

Unknown counts print as `?`.

### `--json` field schema

One JSON object with exactly these twelve keys. A value that does not apply is
`null`.

| Key | Type | Description |
|-----|------|-------------|
| `run_id` | string or null | The run directory name; null until a detached run registers one |
| `unit` | string or null | The detached unit name; null for a foreground run |
| `host` | string or null | The ssh target for a remote run. A local `status` emits null; `status --on HOST` and `wait --on HOST` fill `HOST` when the remote object carries null |
| `state` | string | One of the five states above |
| `exit_code` | integer or null | The recorded exit status; null while `starting`, `running` or `unknown` |
| `run_dir` | string or null | Absolute path of the run directory |
| `log` | string or null | The detached unit's log, or the run's `kas.log` for a foreground run |
| `tasks_done` | integer or null | Tasks finished, from the run's progress data |
| `tasks_total` | integer or null | Tasks planned |
| `tasks_running` | integer or null | Tasks executing now; null without a bitbake event log |
| `elapsed_seconds` | number or null | Seconds since the run started |
| `failure` | object or null | Set only when `state` is `failed`: `{"recipe", "task", "step", "error"}`. A task failure fills `recipe` and `task` (`step` and `error` null). Otherwise `step` and `error` say why: `step` is the first failing step (else the terminal error type) and `error` is the first error line the build printed (bitbake's `ERROR:` line or kas's own, as `ERROR: <message>`, at most 200 characters), captured while the build ran and never read from the log at query time. `error` is `exit_code=N` when the build printed none. When `bakar stop` ended the run, `step` is `stopped` and `error` is `stopped by bakar stop`, with `state` still `failed` (see [Stopped runs](#stopped-runs)). When the pre-flight checks refused the build, `step` is `doctor` and `error` names each failed blocking check as `<check>: <message>` (see [Refused builds](#refused-builds)). Null when the run recorded none of these |

```bash
$ bakar status bakar-detach-20260701-090000-a1b2c3 --json
{"run_id": "20260701-090002-48213", "unit": "bakar-detach-20260701-090000-a1b2c3", "host": null, "state": "running", "exit_code": null, "run_dir": "/ws/build/runs/20260701-090002-48213", "log": "/path/to/runtime/bakar-detach-20260701-090000-a1b2c3.log", "tasks_done": 1204, "tasks_total": 7312, "tasks_running": 24, "elapsed_seconds": 815.2, "failure": null}
```

## Stopped runs

`bakar stop` records its request in a `stop-request.json` file in the run directory, written before the first signal (on the host for `--on`). `status` reads it only to explain a failure: it never changes `state` or `exit_code`, so a stopped run is `failed` with `failure.step` `stopped`.

```json
"failure": {"recipe": null, "task": null, "step": "stopped", "error": "stopped by bakar stop"}
```

A structured task failure wins over the request: when the run recorded a failing task, `failure` names that task and not `stopped`.

Limits, each of which reads as an ordinary failure:

- A run from before this version, which has no request record.
- A stop by `kill` or `systemctl stop`, which writes no request.
- A dispatched unit stopped before it registered its run.
- A stop issued while the host's bakar is older than this version.

A forced `bakar stop --on HOST --force` hard-kills the unit, including the wrapper that writes the exit sentinel. The run can then read `unknown` and `bakar wait` exits 3, because a stop request never decides the state.

## Refused builds

When the pre-flight checks refuse a build, the doctor step records the failed blocking checks on its failing event, joined by a semicolon and a space in the order they ran, as one clean line of at most 200 characters. When several checks fail together each entry is shortened to a fair share and ends with `...`. The complete results stay in `diagnosis.txt` in the run directory and in `bakar doctor`; `status` reads only the event records, never that file.

```json
"failure": {"recipe": null, "task": null, "step": "doctor", "error": "bitbake-locks: bitbake.lock held by PID 4242 - another build is running"}
```

Limits, each of which reads `blocking failure` with no check named:

- A run recorded before this version.
- A build dispatched to a host that runs an older bakar.

## Detail snapshot

`--detail` answers "how is it going" in one call. With `--json` it adds a
thirteenth key, `detail`, to the object above; the twelve keys are unchanged.
`detail` holds exactly these six keys.

```bash
$ bakar status 20260701-090002-48213 --detail --json
{"run_id": "20260701-090002-48213", "unit": null, "host": null, "state": "running", "exit_code": null, "run_dir": "/ws/build/runs/20260701-090002-48213", "log": "/ws/build/runs/20260701-090002-48213/kas.log", "tasks_done": 1204, "tasks_total": 7312, "tasks_running": 24, "elapsed_seconds": 815.2, "failure": null, "detail": {"running_tasks": [{"recipe": "zlib-1.3-r0", "task": "do_compile", "seconds": 212}], "failed_tasks": {"count": 0, "names": []}, "last_line": "NOTE: Running task 1204 of 7312 (zlib-1.3-r0:do_compile)", "disk_free_gb": {"build": 412.3, "sstate": 1830.7}, "mem_available_mb": 48210, "pressure": {"cpu": 12.4, "io": 0.8, "memory": 0.0}}}
```

| Key | Type | Source and when null |
|-----|------|----------------------|
| `running_tasks` | array or null | Only while `state` is `running`, from the bitbake event log: `{recipe, task, seconds}` for the 16 longest-running tasks, longest first. Recipe names are as bitbake records them (package-version-revision, for example `zlib-1.3-r0`), with the same character cleaning as `last_line`. Null in every other state and when the run has no event log |
| `failed_tasks` | object or null | `{count, names}` from the event log. Setscene recoveries are excluded. `names` holds the five most recent failures, oldest first, as `<recipe>:<task>` with the same recipe naming; a missing recipe or task is written `?`. Names pass through the same character cleaning as `last_line`. Null when the run has no event log |
| `last_line` | string or null | The last line of `kas.log`, else of the unit log, that is not empty once cleaned. Escape sequences, control characters, bidirectional controls and line or paragraph separators are removed, and the line is cut to 200 characters. Null when neither log exists or has a visible line. It is informational: it never decides the state, the exit code or a failure |
| `disk_free_gb` | object | `{build, sstate}` in GiB with one decimal. `build` is measured at the recorded `TMPDIR`, else the run's build directory; when the recorded `TMPDIR` does not exist yet (early in a first build) the run's build directory is measured instead. `sstate` is null when no sstate path was recorded, which is every run from before this version. Each value is null when its filesystem does not answer within 2 seconds |
| `mem_available_mb` | integer or null | `MemAvailable` from `/proc/meminfo`; null when unreadable |
| `pressure` | object or null | `{cpu, io, memory}` ten-second averages from `/proc/pressure`. Null, or null per resource, where the host has no PSI |

The task fields describe the run; the host fields do not. `disk_free_gb`,
`mem_available_mb` and `pressure` are measured at query time on the host
running `bakar status`, which under `--on HOST` is the remote. They show the
host now, not the host as it was during the run.

Without `--json`, `--detail` prints the usual summary line and then up to four
lines on stderr, `running:` and `failed:` only when there is something to show.
`?` marks an unknown value. The `running:` line lists the five longest and ends
with `(+N more)`, where N is the real number of running tasks minus the five
shown. The `failed:` line ends with `+K more` when the failure count exceeds the
names kept.

```text
20260701-090002-48213 running tasks=1204/7312 running=2
running: zlib-1.3-r0:do_compile 3m32s, openssl-3.2.1-r0:do_compile 2m10s
host: disk build 412.3G sstate 1830.7G free, mem 48210M available, pressure cpu 12.4 io 0.8 mem 0.0
last: NOTE: Running task 1204 of 7312 (zlib-1.3-r0:do_compile)
```

## Exit status

`status` exits 0 whenever it reports a state, including `failed` and
`unknown`; read `state` or `exit_code` for the outcome. It exits 2 when the
handle resolves to nothing. To turn the outcome into an exit status, use
[`bakar wait`](wait.md).

## Remote runs

`--on HOST` runs `bakar status` on `HOST` over ssh and mirrors its stdout and
exit status. Use it for a run started with `bakar build --on HOST --detach`.
An unreachable host or failed ssh transport exits 2 and names the ssh exit
status (255 for an unreachable host); [`bakar wait`](wait.md) exits 3 for the
same failure. A host starting with `-`, `-w` with `--on`, a missing bakar on
the host, and a bakar whose version differs from the local one exit 2 for both
commands, because the caller must fix the invocation or install bakar there.
A build of the same version with a different content hash (the parenthesised id
in `bakar --version`) prints a warning on stderr and continues, since these
queries only read run records. The handle is sent after `--`, so a handle
beginning with `-` is never read as a flag. With `--json`, a remote object
whose `host` is null gets `HOST` filled in and is re-emitted as one JSON line;
output that is not one JSON object is mirrored
unchanged. The remote call is bounded to 30 seconds.

`--detail --on HOST` against a remote of a different bakar version is refused
with the same version-mismatch message and exit 2, so a mismatched remote never
sees the flag. A same-version remote that lacks `--detail` exits 2 with its own
usage error. Host values in the snapshot are measured on `HOST` in that case.

## Examples

```bash
# Where is the newest run in this workspace?
bakar status

# Poll a detached run from any directory, as JSON
bakar status bakar-detach-20260701-090000-a1b2c3 --json

# The same run, by run id
bakar status 20260701-090002-48213 --json

# A run on another host
bakar status bakar-detach-20260701-090000-a1b2c3 --on pc3 --json
```

## See also

- [wait.md](wait.md) - block until the run finishes and exit with its outcome
- [build.md](build.md) - `--detach` starts a run that outlives your terminal
- [ps.md](ps.md) - list every live build on this host
- [triage.md](triage.md) - post-mortem a failed run
- [agents.md](agents.md) - driving bakar from an agent or a script
