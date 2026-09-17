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
| `--run` | Stop the run whose run directory name exactly matches this id, resolved against every family root in the workspace rather than only the one root/kas YAML would resolve. When no workspace can be found from cwd (and no `--workspace` was given), this pairing composes with the [host-wide fallback](#host-wide-fallback) below instead of failing - `--run` is then resolved against every host-mode build on the host. Find run ids with [`bakar ps`](ps.md) |
| `--on` | Stop the detached build dispatched to this host with `bakar build --on <host>`; resolves nothing locally, so it works from any directory |
| `--all` | With `--on`, stop every detached build on the host instead of refusing when more than one is running |
| `--force` | Skip the SIGINT grace period and escalate straight to the scoped SIGTERM -> SIGKILL reaper. On the [host-wide fallback](#host-wide-fallback), this also skips the confirmation prompt, but only when paired with an explicit `--run <id>` - every other host-wide path still confirms regardless of `--force` |
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

# Stop a build dispatched with `bakar build --on pc2`
bakar stop --on pc2

# Two or more builds live in this workspace: stop one by run id
bakar ps
bakar stop --run 20260701-090000-aaa
```

## Multiple live builds in one workspace

A workspace can have more than one build running at once - a host-mode build
and a container-mode build started separately, or two family roots (NXP and
a BYO kas YAML) both dispatched. `bakar stop` with no `--run` has to decide
which one you mean:

- **Exactly one** live build: stopped directly, no listing, no prompt - the
  same behavior as before this feature existed.
- **Zero** live builds: falls through to the existing single-root
  `stop_build` path unchanged, including its stale-lock cleanup and
  messaging.
- **Two or more** live builds: resolved by `--run <id>`, or by the
  refuse-and-list / interactive pick below.

### `--run <id>`

Pass `--run` with a run directory name (as reported by `bakar ps` or by the
listing below) to stop that specific run, regardless of how many others are
live. It is resolved against every family root in the workspace, not just
the one root the positional `KAS_YAML` / `--manifest` would resolve to - so
it works even when the live build you want to stop was launched from a
different kas YAML than the one you'd otherwise pass. Exits 1 with `no run
matching '<id>' found in this workspace` when nothing matches, or with `run
<id> is not currently live` when the id matches a finished/stale run rather
than a live one.

### Refuse-and-list (non-interactive)

When two or more builds are live and stdin is not a TTY (a script, CI, a
redirected pipe) and `--run` was not given, `bakar stop` refuses to guess.
It lists every live run - run id, family, machine, elapsed time - and exits
1 with a pointer at `bakar stop --run <id>`:

```text
2 live builds are running in this workspace:
  20260701-090000-aaa  family=nxp  machine=imx8mp-var-dart  elapsed=12m30s
  20260701-110000-bbb  family=ti   machine=am62x-sk          elapsed=3m10s
refusing to stop more than one - pick one:  bakar stop --run <id>
```

### Interactive pick

When two or more builds are live and stdin **is** a TTY and `--run` was not
given, `bakar stop` prints the same listing with a number per row and
prompts for a choice instead of refusing:

```text
2 live builds are running in this workspace:
  [1] 20260701-090000-aaa  family=nxp  machine=imx8mp-var-dart  elapsed=12m30s
  [2] 20260701-110000-bbb  family=ti   machine=am62x-sk          elapsed=3m10s
Stop which build [1-2]:
```

An out-of-range choice exits 1 without stopping anything. The choice is
translated to a run id and dispatched through the exact same stop path
`--run` uses - there is no separate interactive stopping logic.

## Host-wide fallback

`bakar stop` normally resolves a workspace from cwd (or from `--workspace`,
or from a positional BYO `KAS_YAML`) before it looks for anything to stop.
When none of those apply - no `--workspace` was passed, this isn't the BYO
case, and the cwd walk finds no workspace - it falls back to scanning every
**host-mode** build on the host instead of failing outright. This is the
same fallback the `--run` row above points at.

The fallback is host-mode only. A container-mode build is filtered out even
when it shares a `bsp_root` with a host-mode build under the same run
directory - the scan looks at each run's recorded launch mode individually,
not just at which topdirs have a live cooker, so a container-mode build
never gets swept in alongside a host-mode sibling.

Confirmation is required on every signal this fallback sends, with exactly
one exception: an explicit `--run <id>` combined with `--force` skips the
prompt outright, because naming the run id and asking for the hard stop in
the same invocation is already the operator's explicit commitment. Every
other combination confirms first - print the candidate's run id, family,
machine, and elapsed time, then ask - including the case where exactly one
host-mode build is found with no selector at all. That single-candidate
case reads the same as the workspace-scoped path above at a glance, but it
is not: a build found host-wide is not guaranteed to belong to the invoking
operator, so it still confirms even though the equivalent workspace-scoped
case does not.

A non-interactive invocation (no TTY on stdin) has no way to answer that
confirmation, so it refuses rather than guessing:

- With no `--run` and two or more live host-mode builds, it refuses and
  lists them, the same shape as [Refuse-and-list](#refuse-and-list-non-interactive)
  above but scoped to the whole host.
- With no `--run` and exactly one live host-mode build, or with an explicit
  `--run <id>` given without `--force`, the confirmation itself is what
  refuses: it prints the candidate row, then `not stopping <id>` and exits 1.

A root the NFS lock-ownership gate could not confirm ownership of is not
silently dropped from the scan - it is diagnosed by name. When another host
holds the root, `bakar stop` names it (`<bsp_root> is owned by <host>; run
'bakar stop' there to check for a live build`); when ownership can't be
confirmed at all, it says so and names the reason instead of pretending the
root was never there.

## Stopping a remote build (`--on <host>`)

A build dispatched with `bakar build --on <host>` runs on the remote under a
transient `bakar-dispatch-*` systemd user unit and deliberately outlives the
terminal that started it, so Ctrl-C there no longer reaches it.
`bakar stop --on <host>` is its kill path.

It needs no workspace, no kas YAML and no run-id: it lists the host's active
`bakar-dispatch-*.service` units over ssh and walks the same ladder the local
stop does - `SIGINT` first, so bitbake drains its running tasks and writes its
run log, then `systemctl --user stop` once `--timeout` seconds (default 30) are
spent. `--timeout 0` waits unbounded, as it does locally. `--force` skips
straight to the hard stop and costs you the run log.

Exits 1 when no detached build is running on that host, matching a local
`bakar stop` with no build to stop.

It stops exactly one build. A host that takes `--on` dispatches is a shared
builder by definition, so a second running `bakar-dispatch-*` unit is somebody
else's work: when more than one is running, bakar lists them and signals
nothing. Pass `--all` to stop every one deliberately.

```bash
# Refuses and lists them when two builds are running on pc2
bakar stop --on pc2

# Stop all of them
bakar stop --on pc2 --all
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
- When a workspace is resolved (from cwd, `--workspace`, or a BYO `KAS_YAML`),
  a build in another workspace is never affected: each workspace has its own
  `build/runs/` and its own `build.pid`. That guarantee is scoped to the
  resolved-workspace path only - the [host-wide fallback](#host-wide-fallback)
  exists precisely because no workspace could be resolved, and it deliberately
  reaches across every host-mode build on the host rather than staying inside
  one workspace's `build/runs/`.

## See also

- [ps.md](ps.md) - list every live build on this host (run id, mode, family, machine) to find the id for `--run`
- [build.md](build.md) - the build pipeline whose run writes `build.pid`
- [hashserv.md](hashserv.md) - the persistent daemon `bakar stop` deliberately leaves running
- [triage.md](triage.md) - post-mortem a build that failed or was interrupted
- [bitbake.md](bitbake.md) - `rebuild` / `clean-recipe` for recovering a corrupted recipe
