# bakar bitbake

Run a single recipe or image target through bitbake inside kas-container, with the run logged.

## Synopsis

```text
bakar bitbake <target> [kas_yaml] [OPTIONS]
bakar clean-recipe <recipe> [kas_yaml] [OPTIONS]
bakar rebuild <recipe> [kas_yaml] [OPTIONS]
```

## Description

`bakar bitbake` is a recipe-level passthrough to bitbake running inside kas-container.
By default it issues `bitbake <target>`; with `--task/-c` it issues `bitbake -c <task> <target>`.
Unlike `bakar shell -c "bitbake <recipe>"`, every invocation is logged to the per-run dir and
exits with bitbake's own exit code, surfacing a non-zero result rather than reporting success.

`bakar clean-recipe` is a thin alias for `bitbake -c cleansstate <recipe>` covering the most
common cleanup task. It shares the same task-execution path, logging, and exit-code behavior.

`bakar rebuild` chains the two into one container invocation:
`bitbake -c cleansstate <recipe> && bitbake <recipe>`. Use it when a recipe's cached output is
stale or corrupt and a plain `bakar bitbake` would just pull the bad sstate. The `&&`
short-circuits, so a failed cleansstate skips the build. `--keep-going/-k` applies to the build
half only.

Two task names are special-cased:

| `--task` value | Behavior |
|----------------|----------|
| `listtasks` | Runs `bitbake -c listtasks <target>`, captures the output, and pretty-prints the parsed `do_*` task names |
| `devshell` | Routes through the interactive path (TTY attached); output is not captured to a log |

Every other invocation captures bitbake's output to a log file under the run dir.

## Workspace dispatch

The BSP family is resolved from how you point bakar at the workspace:

- **BYO / bbsetup**: pass the positional `kas_yaml` (e.g. `meta-avocado/kas/machine/qemux86-64.yml`);
  the workspace is resolved next to it.
- **NXP / TI**: pass `-f/--manifest` (NXP `.xml` or TI `.txt`); the family is dispatched from the
  manifest filename.

Run from inside a workspace and both can be omitted.

## kas-container requirement

bitbake runs inside kas-container, so a synced workspace with a working container image is
required. Run `bakar sync` first if the workspace has not been initialized.

## Cache mount gate

Before any bitbake process is started - for `bakar bitbake`, `bakar clean-recipe`,
`bakar rebuild`, and the `listtasks` task - bakar probes every effective cache
directory (sstate, downloads, ccache) with a 20-second bounded readiness check.
The probe deliberately triggers an idle `systemd.automount` unit (a plain
`stat()`/`os.access()` does not), so a cache share that has been auto-unmounted
since the last build is live again before bitbake ever parses a recipe.

**The interactive `devshell` task bypasses this gate.** `bakar bitbake --task devshell`
routes through `run_shell()` (`src/bakar/steps/kas_build.py`), which never calls
the `cache_mount_refusal()` check that gates the other paths (`run_build`,
`run_shell_live`, `run_shell_capture`). This is a real, deliberate gap, not an
oversight fixed elsewhere: a devshell launched against a wedged NFS cache mount
can hang instead of refusing with the readiness error above.

If a directory is unresponsive, errored, or declared NFS in `/etc/fstab` but
resolves to local disk, the launch refuses immediately: no bitbake process is
started. The refusal names the unusable directory and its server, for example:

```text
sstate /srv/cache/sstate (cache.example.com): did not answer within 20s; check the
NFS server and this node's link; a client that stays wedged after the server
returns needs its mounts force-unmounted or a reboot
```

This closes a real, previously always-reproducible failure: a cache directory
sitting behind an idle automount used to fail bitbake's own `DL_DIR` sanity check
("exists but you do not appear to have write access to it") because bitbake's
own check never triggers the mount. The gate's `stat -f` probe does trigger it,
so a build that used to fail on a cold cache share now succeeds without any
manual `ls`/`cd` warm-up.

When every cache directory is healthy, the same gate also refuses the launch if
the workspace's hashserv/prserv state directory would land on a network
filesystem - see [hashserv.md](hashserv.md) and [prserv.md](prserv.md).

## Run logging

Each non-interactive invocation writes its captured output to
`<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/` as `bitbake.log` (for `bakar bitbake`),
`clean-recipe.log` (for `bakar clean-recipe`), or `rebuild.log` (for `bakar rebuild`). Use
`bakar log` to inspect them. The `devshell` path is interactive and produces no captured log.

### Failure detail on the live console

When a run hits an `ERROR:`/`FATAL:` line, the live console now forwards the
indented reason lines that follow it - up to 20 lines - instead of showing only
the bare header. This is what surfaces a message like "exists but you do not
appear to have write access to it" directly on screen rather than leaving you
to open the log to find it. Once 20 continuation lines have been shown, a
`... (more lines in kas.log)` marker appears and the rest is left in the run's
`bitbake.log`/`kas.log` for `bakar log` or `bakar triage` to inspect.

## Options

### `bakar bitbake`

| Flag | Short | Description |
|------|-------|-------------|
| `--task` | `-c` | bitbake task to run (e.g. `compile`, `listtasks`, `devshell`); omit to run the default build |
| `--keep-going` | `-k` | Pass `-k` to bitbake (keep building after failures) |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |

### `bakar clean-recipe`

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |

`clean-recipe` has no `--task` or `--keep-going`; its task is fixed to `cleansstate`.

### `bakar rebuild`

| Flag | Short | Description |
|------|-------|-------------|
| `--keep-going` | `-k` | Pass `-k` to the build half (keep building after failures); does not affect cleansstate |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |

`rebuild` has no `--task`; it always runs `cleansstate` then the default build.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | bitbake completed successfully |
| 2 | No workspace found from the current directory and no `--workspace` given |
| other | bitbake exited non-zero (propagated verbatim) |

## Examples

```bash
# Build busybox in a BYO/bbsetup workspace
bakar bitbake busybox meta-avocado/kas/machine/qemux86-64.yml

# Run only the compile task
bakar bitbake busybox --task compile meta-avocado/kas/machine/qemux86-64.yml

# List the available tasks for a recipe
bakar bitbake busybox --task listtasks meta-avocado/kas/machine/qemux86-64.yml

# Drop into an interactive devshell
bakar bitbake busybox --task devshell meta-avocado/kas/machine/qemux86-64.yml

# Keep building after a failure
bakar bitbake core-image-minimal --keep-going meta-avocado/kas/machine/qemux86-64.yml

# NXP workspace via manifest dispatch
bakar bitbake busybox -f imx-6.12.49-2.2.0.xml

# Clean a recipe's sstate
bakar clean-recipe busybox meta-avocado/kas/machine/qemux86-64.yml

# Rebuild a recipe from scratch (cleansstate, then build)
bakar rebuild qtwebengine meta-avocado/kas/machine/rzv2h-rdk.yml
```

## See also

- [inspect.md](inspect.md) - deep per-recipe inspection report before building
- [graph.md](graph.md) - dependency-graph analysis from `bitbake -g` output
- [shell.md](shell.md) - drop into the container to run bitbake tooling directly
- [log.md](log.md) - tail the run logs
- [sync.md](sync.md) - sync sources before running container-backed commands
