# bakar stop

Gracefully halt a running `bakar build` without corrupting in-flight recipe workdirs.

## Synopsis

```text
bakar stop [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--force` | Skip the SIGINT grace period and escalate straight to SIGTERM, then SIGKILL |
| `--manifest`, `-f` | Manifest filename used to resolve the BSP family (when cwd auto-detection is ambiguous) |
| `--workspace`, `-w` | Workspace root override |

## Examples

```bash
# Stop the running build for the workspace detected from cwd
bakar stop

# Stop a build in a workspace outside the current directory
bakar stop --workspace ~/bsp/my-workspace --manifest imx-6.12.49-2.2.0.xml

# Hard stop: skip the graceful SIGINT wait
bakar stop --force
```

## What it does

`bakar build` records the build's process-group id in `build.pid` inside the run
directory (`<bsp_root>/build/runs/<run_id>/build.pid`) the moment kas-container
starts. `bakar stop` reads that file for the latest run and signals **only** that
process group:

1. Sends `SIGINT` to the build process group. bitbake handles SIGINT with its own
   graceful shutdown ("waiting for N running tasks to finish"), letting running
   tasks complete and writing consistent sstate.
2. Waits up to 60 seconds for the group to exit.
3. If still alive, escalates to `SIGTERM`, waits 5 seconds, then `SIGKILL`.

`--force` skips step 1 and the grace wait, sending `SIGTERM` immediately followed
by `SIGKILL`.

Before signalling, `bakar stop` verifies that the recorded PGID belongs to a
kas-container process (`/proc/<pgid>/cmdline`). If the PID is dead or has been
recycled to an unrelated process, it prints `no running build found`, clears the
stale `build.pid`, and exits 0 without signalling anything.

## Why graceful matters

Killing a build with `SIGKILL` / `pkill -9` mid-compile leaves the in-flight
recipe's workdir inconsistent, and the corruption does not self-heal on a later
`bakar build` resume - only `bitbake -c cleansstate <recipe>` recovers it. Routing
through bitbake's own SIGINT shutdown avoids that corruption and keeps the build
resumable: a subsequent `bakar build` continues from sstate without any manual
cleansstate.

## Unclean-stop detection

If a build was killed without `bakar stop` (a raw `kill -9`, a power loss, an OOM
kill), the `build.pid` is left behind with a dead PGID. The next `bakar build`
detects this at startup and prints a warning naming the interrupted step and
pointing you at `kas.log` in that run directory for the recipe that was building.
The warning is advisory - it never blocks or auto-repairs the build. If the named
recipe fails to rebuild with non-self-healing errors, run
`bitbake -c cleansstate <recipe>` (via `bakar shell` or `bakar rebuild <recipe>`).

## Scope and safety

- `bakar stop` signals only the process group recorded for the target workspace's
  latest run. It never uses `pkill` and never matches on process names or paths.
- It leaves the persistent `bitbake-hashserv` daemon untouched - that daemon is
  shared and long-lived. Use [`bakar hashserv stop`](hashserv.md) to stop it
  deliberately.
- A build in another workspace is never affected: each workspace has its own
  `build/runs/` and its own `build.pid`.

## See also

- [build.md](build.md) - the build pipeline whose run writes `build.pid`
- [hashserv.md](hashserv.md) - the persistent daemon `bakar stop` deliberately leaves running
- [triage.md](triage.md) - post-mortem a build that failed or was interrupted
- [bitbake.md](bitbake.md) - `rebuild` / `clean-recipe` for recovering a corrupted recipe
