# Driving bakar from an agent or a script

A bakar build outlives a single tool call, so launch it detached and follow it by handle. Do not wrap it in shell job control and do not scrape processes.

## Lifecycle

1. Launch: `bakar build <kas.yml> -t <target> --detach`. Add `--on HOST` to build on another node. Keep the handle the command prints.
2. Wait: `bakar wait <handle> [--on HOST] --timeout 540`. Repeat the call while it exits 124.
3. Progress: `bakar status <handle> --json`. `tasks_total` gives the task total once bitbake has reported it.
4. Conclude: `bakar triage <run_id> --json` for a failure, `bakar report <run_id> --json` for a finished run. For an `--on` build the form is `bakar triage <run_id> --on <host> --json`. A registered run id resolves from any directory, so no workspace is needed.
5. Stop: `bakar stop <kas.yml> --run <run_id>`. With `--on HOST`, `bakar stop` ignores `--run`, so use `bakar stop --on HOST`. For meta-avocado builds `bakar stop` needs the same kas YAML used to start the build, the way `bakar build` does, to resolve the BSP family.

The handle is either the detached unit name or the run id. The run id is registered a moment after launch, so a status read right after `--detach` may report `starting` with `run_id` null.

## Exit statuses of `bakar wait`

| Exit | Meaning | Next step |
|------|---------|-----------|
| 0 | build succeeded | `bakar report <run_id> --json` |
| 1 | build failed | `bakar triage <run_id> --json` |
| 3 | unknown: no recorded outcome and nothing alive (also an ssh transport failure under `--on`) | `bakar status <handle> --json` |
| 124 | still starting or running when `--timeout` elapsed | call `bakar wait` again |
| 2 | handle does not resolve, or usage error | fix the handle or arguments |

Progress lines go to stderr, one per poll (`--interval`, default 30 seconds, minimum 1). With `--json` the final status object is the only stdout. Keep `--timeout` at 540 so each call stays under typical tool timeouts.

## JSON shapes

`bakar status <handle> --json` prints one object. `bakar wait --json` prints the same object for the last poll.

```json
{
  "run_id": "20261019-150000-4242",
  "unit": "bakar-detach-20261019-150000-4242",
  "host": null,
  "state": "running",
  "exit_code": null,
  "run_dir": "/path/to/build/runs/20261019-150000-4242",
  "log": "/path/to/runtime/dir/bakar-detach-20261019-150000-4242.log",
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
- `failure` is null unless `state` is `failed`; then it is `{"recipe": ..., "task": ..., "step": ..., "error": ...}`. A task failure sets `recipe` and `task`. A run that failed before any task ran (a bad `--target`, a missing kas file) sets `step` to the failing step and `error` to the recorded error text (null when none was recorded), with `recipe` and `task` null. It is null when the run recorded neither.
- `host` is the ssh target for a run queried with `--on`; a local query leaves it null.
- `bakar status` exits 0 whenever it reports a state and 2 when the handle does not resolve. It never reflects the build outcome in its exit status; read `state`.

Without `--json` the same record prints as one line: `<id> <state> [exit=N] tasks=<done>/<total> running=<n> [failed=<recipe>:<task> or failed=<step>]`, with `?` for unknown values.

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
| `ssh HOST 'bakar build ...'` | `bakar build <kas.yml> --on HOST --detach` |
| `kas build` or `kas-container build` | `bakar build <kas.yml>` |
| `tee` on build output | `bakar log` or `bakar monitor` |

## Hints under Claude Code

When `CLAUDECODE=1` is set, bakar writes next-step hints to stderr. Nothing else reads the variable, and stdout stays machine-readable.

Each hint line starts with `hint:` and wraps its command in backticks.

- A foreground `bakar build` is nudged to use `--detach` and then `bakar wait <run_id> --timeout 540` to avoid tool timeouts.
- A detached launch prints the wait, status and stop commands for the handle. Under `--on HOST` each carries `--on HOST` and the stop line is `bakar stop --on HOST`. Before the run id is registered, a line saying that `bakar status <handle> --json` names the run id replaces the stop line.
- A finished run prints `hint: next:` followed by `bakar triage <run_id> --json` on failure or `bakar report <run_id> --json` on success.
