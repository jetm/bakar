# bakar stop

Gracefully halt a running `bakar build` without corrupting in-flight recipe workdirs.

## Synopsis

```text
bakar stop [OPTIONS] [KAS_YAML]
```

## Arguments

| Argument | Description |
|----------|-------------|
| `KAS_YAML` | Optional kas YAML for a BYO/generic build; runs live next to it under `<yaml-parent>/build/runs/`, and the workspace lookup is skipped (mirrors `bakar build` / `bakar log`) |

## Options

| Flag | Description |
|------|-------------|
| `--force` | Skip the SIGINT grace period and escalate straight to the scoped SIGTERM -> SIGKILL reaper |
| `--timeout` | Auto-escalate after this many seconds of graceful waiting instead of waiting for a Ctrl-C. Defaults to `[build] stop_grace_seconds` (30s); `0` waits unbounded |
| `--manifest`, `-f` | Manifest filename used to resolve the BSP family (NXP/TI); mutually exclusive with a positional `KAS_YAML` |
| `--workspace`, `-w` | Workspace root; changes directory into the resolved workspace before resolving paths, so a relative `KAS_YAML` resolves from outside the workspace. An invalid path exits 2 |

## Examples

```bash
# Stop the running build for the workspace detected from cwd (NXP/TI)
bakar stop

# Stop a BYO/generic build by pointing at its kas YAML
bakar stop examples/kas-qemux86-64-wrynose.yml

# Stop a build in a workspace outside the current directory
bakar stop --workspace ~/bsp/my-workspace --manifest imx-6.12.49-2.2.0.xml

# Hard stop: skip the graceful SIGINT wait
bakar stop --force

# Bound the graceful wait explicitly (auto-escalate after 10s)
bakar stop --timeout 10
```

## What it does

`bakar build` records the run's execution mode the moment the build starts:
`build.pid` holds the kas-container wrapper's process-group id, and
`build.meta.json` captures the mode (`host` or `container`), the container
runtime, and the `bakar.run_id=<run_id>` label injected into the container.
`bakar stop` reads that record for the latest run
(`<bsp_root>/build/runs/<run_id>/`) and dispatches on mode.

**Container builds** (the default - kas-container under docker/podman): bitbake
runs under the runtime daemon in a process tree separate from the wrapper, so
signalling the wrapper PGID would orphan it. `bakar stop` instead:

1. Resolves the container by its label
   (`docker|podman ps -q -f label=bakar.run_id=<run_id>`).
2. Sends `SIGINT` to the **main bitbake process inside the container**
   (`<runtime> exec <cid> pkill -INT -f 'bin/bitbake '`). bitbake runs its
   graceful shutdown ("Keyboard Interrupt, closing down..."), letting running
   tasks finish and writing consistent sstate. The container's PID 1 is not
   signalled: the kas-container entrypoint runs under docker-init and does not
   forward signals to bitbake, so the SIGINT goes straight to the cooker. If the
   in-container exec fails, it falls back to a PID-1 SIGINT.
3. Waits, unbounded and task-aware, until the container is no longer running
   (see [Graceful wait](#graceful-wait)). There is no fixed 60-second cap.
4. On Ctrl-C or after `--timeout` seconds, escalates to `<runtime> stop --timeout=5`
   (SIGTERM), `<runtime> kill --signal=SIGKILL`, then `<runtime> rm -f` to
   force-remove a wedged container so it cannot block the next build.

**Host builds** (`bakar --host build` - plain `kas` on the host, no container):
bitbake is a real descendant of the wrapper, so `bakar stop` signals the recorded
process group directly:

1. Sends `SIGINT` to the build process group and to bitbake-server's own detached
   PID (read from `bitbake.lock`); bitbake runs its graceful shutdown.
2. Waits, task-aware, until every part of the build is gone (see
   [Graceful wait](#graceful-wait)).
3. On Ctrl-C or after `--timeout` seconds, runs the scoped SIGTERM -> SIGKILL
   reaper (see [Forced cleanup and verification](#forced-cleanup-and-verification)).

   Before signalling, it verifies the recorded PGID still belongs to a
   kas-container/kas process (`/proc/<pgid>/cmdline`). When the wrapper is dead
   *and* no detached cooker survives, `bakar stop` clears any stale
   `bitbake.lock` / `bitbake.sock` and exits 0 (an idempotent clean-tree no-op).
   A wrapper that is gone while a detached cooker still holds the build is
   escalated against directly.

`--force` skips the SIGINT step in both modes and escalates straight to the scoped
SIGTERM -> SIGKILL reaper.

## Graceful wait

The SIGINT grace wait is task-aware and, by default, bounded by
`[build] stop_grace_seconds` (30s, overridable per-invocation with `--timeout`;
set to `0` to wait unbounded). After the SIGINT, `bakar stop` waits until the build
process (host) or container (container mode) is no longer running, so a long
`do_compile` is allowed to finish and write consistent sstate rather than being cut
off at a fixed 60-second cap. The bound exists so a *wedged* cooker - one whose
client fds are dead but never reaped, leaving the server waiting on them forever -
cannot deadlock `bakar stop` when no operator is present to press Ctrl-C.

While it waits, it renders live progress from the build's event log
(`bitbake_eventlog.json`): `Waiting for N running tasks to finish (elapsed …)` with
one `recipe:task elapsed` row per running task. When task progress is unavailable -
no event log, a malformed or truncated log, or the log stops updating during the
drain - it falls back to a spinner and elapsed timer with a periodic
`still waiting; press Ctrl-C to force` hint plus the alive PID / container id.

Pressing Ctrl-C during the wait, or the `--timeout` elapsing, escalates immediately
to the scoped SIGTERM -> SIGKILL reaper. `--force` skips the graceful wait entirely.

In container mode, a liveness query that errors (docker/podman transiently
unreachable) is not treated as "container stopped"; `bakar stop` warns and keeps
waiting. If the runtime stays unreachable across repeated liveness queries,
`bakar stop` gives up and exits 1 with `lost contact with the container runtime`.

## Forced cleanup and verification

Escalation on a host build is scoped to **this build only** - it never touches
another workspace's cooker on the same host. `bakar stop` identifies the target
process set by the build directory: bitbake-server is spawned with this build's
`bitbake.lock`, `bitbake.sock`, and `bitbake-cookerdaemon.log` paths in its argv,
so a scan of `/proc/<pid>/cmdline` for those exact paths finds the wedged cooker
even when its `bitbake.lock` first line is unreadable and the PGID can no longer
reach it. From that seed set - plus the recorded wrapper process group - it walks
`/proc` parent links to gather the whole tree: the cooker, its `bitbake-worker`
processes, task subprocesses, and any children reparented to init. The `bakar stop`
process and its own group are always excluded, so it can never signal itself.

Every gathered process gets SIGTERM, a short grace window, then SIGKILL for
whatever survives. Each signalled PID is logged (`SIGTERM pid … (cmdline)`) so the
action is auditable. Once the tree is gone, the stale `bitbake.lock` / `bitbake.sock`
are removed - but only after confirming no live process still holds them, so a lock
is never yanked out from under a running cooker.

`bakar stop` reports success only after **verifying** the cleanup: zero remaining
cooker/worker processes, a dead wrapper process group and bitbake-server PID, no
build container, and both `bitbake.lock` and `bitbake.sock` gone. If anything
survives (for example a process it lacks permission to kill), it prints
`stop incomplete` with the specific remainders and exits 1.

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

- `bakar stop` targets only the recorded run for the workspace: container builds
  by their unique `bakar.run_id` label, host builds by the recorded process group
  and by the argv-scoped `/proc` scan for this build's `bitbake.lock` /
  `bitbake.sock` / `bitbake-cookerdaemon.log` paths. Because a second build on the
  same host has a different build directory, its cooker's argv references
  different paths and can never match - a concurrent build is left untouched. The
  container `pkill` runs **inside** the resolved container only, and the label is
  per-run so it cannot collide with another build.
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
