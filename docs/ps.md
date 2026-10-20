# bakar ps

List the `bakar` builds running on this host, across every workspace: the
bitbake cookers of host-mode builds, the containers of container-mode builds,
and the detached or dispatched units that are active but have not started
bitbake yet.

## Synopsis

```text
bakar ps [OPTIONS]
```

Directory-independent: unlike every other `bakar` command, `ps` performs no
workspace resolution at all. It never looks for a `.bakar.toml` or a family
root, so it runs correctly from anywhere on the host, including outside any
bakar workspace.

## Options

| Flag | Description |
|------|-------------|
| `--json` | Emit a JSON array to stdout instead of plain text |
| `--on HOST` | List the builds on `HOST` over ssh instead of locally. The host runs its own `bakar ps` and its stdout and exit status are mirrored; a failed ssh transport exits 2. There is no `-w`, since `ps` takes no workspace |

## What it finds

`bakar ps` combines three independent discovery sources and de-duplicates
across them. Each row's `mode` names the source that found it:

- **Host-mode builds** (`mode` `host`) - found by scanning `/proc` for
  bitbake cooker processes and correlating each one to its live run directory.
- **Container-mode builds** (`mode` `container`) - found by querying the
  container runtime (docker or podman) for every running container carrying a
  `bakar.run_id` label, collapsed to one row per run id even when more than
  one container shares that label.
- **Units that have not started bitbake** (`mode` `unit`) - found in the
  runtime directory: every detached or dispatched unit that systemd reports
  active and that has no exit sentinel yet. The row's `run_id` is the run id
  the unit registered last, or the unit name when it has registered none. A
  `unit` row shows `unknown` family and machine, because no run record names
  them before bitbake starts. A unit whose run id already appears as a `host`
  or `container` row is not listed again, so the same build switches from a
  `unit` row to a cooker or container row once bitbake starts.

A run id reported by both the cooker scan and the container runtime (a
container build whose cooker is also visible on the host `/proc` tree) is
rendered only once, as its container row - the more actionable of the two.

A foreground build run locally, with no `--detach` and no `--on`, registers
no unit, so it appears only once its cooker starts; while it runs doctor,
sync or checkout it is not listed. A foreground `bakar build --on HOST` always
registers a unit on the host, so `bakar ps --on HOST` lists it from the start.

`bakar ps` is read-only: it never signals a process, stops a container, or
touches a run directory. Use [`bakar stop`](stop.md) to stop a build once
you've found its run id here.

## Output

Plain text, one row per live build:

```text
20260701-090000-48213  mode=host  family=nxp  machine=imx8mp-var-dart  elapsed=12m30s
20260701-120000-52304  mode=container  family=unknown  machine=unknown  elapsed=3m10s
20260701-130002-61027  mode=unit  family=unknown  machine=unknown  elapsed=40s
```

With no live builds:

```text
no bakar builds running
```

### `--json` field schema

Each row is a JSON object with exactly these five fields - no more, no
fewer, and this is the frozen schema `bakar ps --json` output must match:

| Field | Type | Description |
|-------|------|--------------|
| `run_id` | string | The run directory name (also the container's `bakar.run_id` label). On a `unit` row, the run id the unit registered last, or the unit name when it has registered none |
| `mode` | string | `"host"`, `"container"` or `"unit"` |
| `family` | string | BSP family (`nxp`, `ti`, `bbsetup`, ...), or the placeholder `"unknown"` for a `unit` row and when a container row's build directory could not be recovered |
| `machine` | string | The resolved MACHINE, or the placeholder `"unknown"` under the same condition as `family` |
| `elapsed_seconds` | integer | Seconds since the run started, or the placeholder `0` when the start time could not be determined |

`family` and `machine` are always `"unknown"` on a `unit` row. They fall
back to `"unknown"` on a container-mode row whose `/work` bind-mount source
can't be resolved (the runtime inspect call failed or timed out), or whose
recovered mount source holds no run record matching that container's run id.
`elapsed_seconds` falls back to `0` when the run directory is unavailable or
its name doesn't parse as a timestamp, which includes a `unit` row whose unit
has registered no run. An empty result emits `[]`.

```bash
$ bakar ps --json
[{"run_id": "20260701-090000-48213", "mode": "host", "family": "nxp", "machine": "imx8mp-var-dart", "elapsed_seconds": 750}]
```

## Examples

```bash
# List the builds on this host, from any directory
bakar ps

# Machine-readable form, e.g. for scripting bakar stop --run <id>
bakar ps --json

# The builds on a host a build was dispatched to
bakar ps --on pc3 --json

# Find the run id of a stuck build, then stop it by id
bakar ps
bakar stop --run 20260701-090000-48213
```

## See also

- [stop.md](stop.md) - stop a build, including the `--run <id>` flag this command's output feeds
- [monitor.md](monitor.md) - live watch of a single running build (cluster load, dist stats, task progress) rather than a host-wide listing
- [triage.md](triage.md) - post-mortem a build that failed or was interrupted
