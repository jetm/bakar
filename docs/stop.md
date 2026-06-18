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
| `--force` | Skip the SIGINT grace period and escalate straight to SIGTERM, then SIGKILL |
| `--manifest`, `-f` | Manifest filename used to resolve the BSP family (NXP/TI); mutually exclusive with a positional `KAS_YAML` |
| `--workspace`, `-w` | Workspace root override |

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
3. Waits up to 60 seconds for the container to exit, polling once a second.
4. If still running, escalates to `<runtime> stop --timeout=5` (SIGTERM), then
   `<runtime> kill --signal=SIGKILL`.

**Host builds** (`bakar build --host` - plain `kas` on the host, no container):
bitbake is a real descendant of the wrapper, so `bakar stop` signals the recorded
process group directly:

1. Sends `SIGINT` to the build process group; bitbake runs its graceful shutdown.
2. Waits up to 60 seconds for the group to exit.
3. If still alive, escalates to `SIGTERM`, waits 5 seconds, then `SIGKILL`.

   Before signalling, it verifies the recorded PGID still belongs to a
   kas-container/kas process (`/proc/<pgid>/cmdline`). A dead or recycled PID
   prints `no running build found`, clears the stale record, and exits 0 without
   signalling anything.

`--force` skips the SIGINT step in both modes and escalates straight to SIGTERM
then SIGKILL.

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
  by their unique `bakar.run_id` label, host builds by the recorded process
  group. The `pkill` runs **inside** the resolved container and matches the
  bitbake UI cmdline there only - it never touches host processes, and the label
  is per-run so it cannot collide with another build.
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
