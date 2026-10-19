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
| `failure` | object or null | Set only when `state` is `failed`: `{"recipe", "task", "step", "error"}`. A task failure fills `recipe` and `task` (`step` and `error` null). A run that failed before any task ran has `recipe` and `task` null, `step` the first failing step (else the terminal error type) and `error` the recorded error text, null when none was recorded. Null when the run recorded neither |

```bash
$ bakar status bakar-detach-20260701-090000-a1b2c3 --json
{"run_id": "20260701-090002-48213", "unit": "bakar-detach-20260701-090000-a1b2c3", "host": null, "state": "running", "exit_code": null, "run_dir": "/ws/build/runs/20260701-090002-48213", "log": "/path/to/runtime/bakar-detach-20260701-090000-a1b2c3.log", "tasks_done": 1204, "tasks_total": 7312, "tasks_running": 24, "elapsed_seconds": 815.2, "failure": null}
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
the host, and a bakar version that differs from the local one exit 2 for both
commands, because the caller must fix the invocation or install bakar there.
With `--json`, a remote object whose `host` is null gets `HOST` filled in and
is re-emitted as one JSON line; output that is not one JSON object is mirrored
unchanged. The remote call is bounded to 30 seconds.

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
