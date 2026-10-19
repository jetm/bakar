# Driving bakar from an agent or a script

A bakar build outlives a single tool call, so launch it detached and follow it by handle. Do not wrap it in shell job control and do not scrape processes.

## Lifecycle

1. Launch: `bakar build <kas.yml> -t <target> --detach`. Add `--on HOST` to build on another node. Keep the handle the command prints.
   - With `--on HOST`, exit 3 means the dispatcher could not confirm within 60 seconds that the remote build started. The handle is still printed. A remote build can also return 3 itself, so run `bakar status <unit> --on HOST` before dispatching again.
   - With `--on HOST`, exit 2 with "cannot create a user unit" means that host cannot detach and nothing was built; run without `--detach` or enable the user manager there.
2. Wait: `bakar wait <handle> [--on HOST] --timeout 540`. Repeat the call while it exits 124.
3. Progress: `bakar status <handle> --detail --json`. One call answers state, task counts, running and failed tasks, the last log line, disk, memory and pressure. `tasks_total` gives the task total once bitbake has reported it. With `--on HOST` the host values are measured on the remote host.
4. Conclude: read `failure.step` first; `stopped` means the build was cancelled on purpose (see [Reading a failure](#reading-a-failure)). Otherwise `bakar triage <run_id> --json` for a failure, `bakar report <run_id> --json` for a finished run. For an `--on` build the form is `bakar triage <run_id> --on <host> --json`. A registered run id resolves from any directory, so no workspace is needed.
5. Stop: `bakar stop <kas.yml> --run <run_id>`. With `--on HOST`, `bakar stop` ignores `--run`, so use `bakar stop --on HOST`. For meta-avocado builds `bakar stop` needs the same kas YAML used to start the build, the way `bakar build` does, to resolve the BSP family. A stopped run reads `failed` with `failure.step` `stopped`; a `kill` or `systemctl stop` records nothing and looks like an ordinary failure.

The handle is either the detached unit name or the run id. The run id is registered a moment after launch, so a status read right after `--detach` may report `starting` with `run_id` null.

## Exit statuses of `bakar wait`

| Exit | Meaning | Next step |
|------|---------|-----------|
| 0 | build succeeded | `bakar report <run_id> --json` |
| 1 | build failed, or was stopped with `bakar stop` (read `failure.step`) | `bakar triage <run_id> --json` unless `failure.step` is `stopped` |
| 3 | unknown: no recorded outcome and nothing alive (also an ssh transport failure under `--on`) | `bakar status <handle> --json` |
| 124 | still starting or running when `--timeout` elapsed | call `bakar wait` again |
| 2 | handle does not resolve, or usage error | fix the handle or arguments |

Progress lines go to stderr, one per poll (`--interval`, default 30 seconds, minimum 1). With `--json` the final status object is the only stdout. Keep `--timeout` at 540 so each call stays under typical tool timeouts.

## JSON shapes

`bakar status <handle> --json` prints one object. `bakar wait --json` prints the same object for the last poll.

```json
{
  "run_id": "20261019-150000-4242",
  "unit": "bakar-detach-20261019-150000-a1b2c3",
  "host": null,
  "state": "running",
  "exit_code": null,
  "run_dir": "/path/to/build/runs/20261019-150000-4242",
  "log": "/path/to/runtime/dir/bakar-detach-20261019-150000-a1b2c3.log",
  "tasks_done": 812,
  "tasks_total": 4310,
  "tasks_running": 6,
  "elapsed_seconds": 931.4,
  "failure": null
}
```

- `state` is one of `starting`, `running`, `succeeded`, `failed`, `unknown`.
- `succeeded` and `failed` come only from a recorded exit status, which is `exit_code`. Liveness only separates `running` from `unknown`.
- `unit` is null for a run that was not detached. `run_id`, `run_dir` and the task counts are null until they exist.
- `failure` is null unless `state` is `failed`; then it is `{"recipe": ..., "task": ..., "step": ..., "error": ...}`. A task failure sets `recipe` and `task`. Otherwise `step` is the failing step and `error` is the first error line the build printed (or `exit_code=N` when it printed none), with `recipe` and `task` null. When `bakar stop` ended the run, `step` is `stopped`. It is null when the run recorded none of these.
- `host` is the ssh target for a run queried with `--on`; a local query leaves it null.
- `bakar status` exits 0 whenever it reports a state and 2 when the handle does not resolve. It never reflects the build outcome in its exit status; read `state`.

A failed run whose build printed an error line:

```json
"failure": {"recipe": null, "task": null, "step": "kas_build", "error": "ERROR: Nothing PROVIDES 'no-such-recipe'"}
```

A run ended by `bakar stop`:

```json
"failure": {"recipe": null, "task": null, "step": "stopped", "error": "stopped by bakar stop"}
```

`bakar status <handle> --detail --json` prints the same object plus a `detail` key. Host values (`disk_free_gb`, `mem_available_mb`, `pressure`) are measured when you ask, on the host running `bakar status`, which is the remote under `--on HOST`. `last_line` is informational; read `state` for the outcome. See [status.md](status.md#detail-snapshot) for each key and when it is null.

```json
{
  "detail": {
    "running_tasks": [{"recipe": "zlib-1.3-r0", "task": "do_compile", "seconds": 212}],
    "failed_tasks": {"count": 0, "names": []},
    "last_line": "NOTE: Running task 812 of 4310 (zlib-1.3-r0:do_compile)",
    "disk_free_gb": {"build": 412.3, "sstate": 1830.7},
    "mem_available_mb": 48210,
    "pressure": {"cpu": 12.4, "io": 0.8, "memory": 0.0}
  }
}
```

Without `--json` the same record prints as one line: `<id> <state> [exit=N] tasks=<done>/<total> running=<n> [failed=<recipe>:<task> or failed=<step>]`, with `?` for unknown values.

## Reading a failure

When `failure.step` is `stopped`, the build was cancelled on purpose. Report it; do not diagnose, fix or relaunch unless asked.

For any other failure, `failure.error` carries the cause, and `bakar triage <run_id> --json` gives a `fail_reason` equal to it.

A run recorded before this version, or one ended by `kill` or `systemctl stop`, cannot be told from an ordinary failure: its `failure.error` is `exit_code=N` and `failure.step` is never `stopped`.

## Where to build

Local versus `--on HOST` is the user's call. Do not pick a node unprompted.

Run from the meta-avocado checkout or worktree and pass the kas YAML as a path relative to it. `-t/--target` names the recipe or image to build.

## Instead of

| Instead of | Use |
|------------|-----|
| `nohup`, `setsid`, a trailing `&` or `timeout` around a build | `--detach` |
| `pgrep` or `ps \| grep` for a build | `bakar ps --json`, `bakar status <handle> --json` |
| `kill` or `pkill` on bitbake or kas | `bakar stop <kas.yml> --run <run_id>` |
| reading `log.do_*` files | `bakar triage <run_id> --json` |
| `tail` or `grep` on a build log | `bakar status <handle> --detail [--on HOST]` |
| `grep -c FAILED` over a build log | `bakar status <handle> --detail [--on HOST]` |
| `df` or `free` on the build host | `bakar status <handle> --detail [--on HOST]` |
| `ssh HOST` probes for build progress | `bakar status <handle> --detail --on HOST` |
| `ssh HOST 'bakar build ...'` | `bakar build <kas.yml> --on HOST --detach` |
| `kas build` or `kas-container build` | `bakar build <kas.yml>` |
| `tee` on build output | `bakar log` or `bakar monitor` |

## Hints under Claude Code

Bakar writes next-step hints to stderr, and stdout stays machine-readable. `CLAUDECODE=1` is the only variable it reads for them, and it gates two of the three kinds:

Each hint line starts with `hint:` and wraps its command in backticks.

- Needs `CLAUDECODE=1`: a foreground `bakar build` (with or without `--on HOST`), `bakar bitbake` or `bakar rebuild` is nudged to use `--detach` and then `bakar wait <run_id> --timeout 540` to avoid tool timeouts.
- Printed whether or not `CLAUDECODE` is set: a detached launch prints the wait, status and stop commands for the handle. Under `--on HOST` each carries `--on HOST` and the stop line is `bakar stop --on HOST`. Before the run id is registered, a line saying that `bakar status <handle> --json` names the run id replaces the stop line.
- Needs `CLAUDECODE=1`: a finished run prints `hint: next:` followed by `bakar triage <run_id> --json` on failure or `bakar report <run_id> --json` on success.
