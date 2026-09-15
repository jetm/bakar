# bakar ps

List every live `bakar` build on this host, across every workspace, in host
mode and container mode alike.

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

## What it finds

`bakar ps` combines two independent discovery sources and de-duplicates
across them:

- **Host-mode builds** - found by scanning `/proc` for bitbake cooker
  processes and correlating each one to its live run directory.
- **Container-mode builds** - found by querying the container runtime
  (docker or podman) for every running container carrying a
  `bakar.run_id` label, collapsed to one row per run id even when more than
  one container shares that label.

A run id reported by both sources (a container build whose cooker is also
visible on the host `/proc` tree) is rendered only once, as its container row
- the more actionable of the two.

`bakar ps` is read-only: it never signals a process, stops a container, or
touches a run directory. Use [`bakar stop`](stop.md) to stop a build once
you've found its run id here.

## Output

Plain text, one row per live build:

```text
20260701-090000-aaa  mode=host  family=nxp  machine=imx8mp-var-dart  elapsed=12m30s
20260701-120000-ccc  mode=container  family=unknown  machine=unknown  elapsed=3m10s
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
| `run_id` | string | The run directory name (also the container's `bakar.run_id` label) |
| `mode` | string | `"host"` or `"container"` |
| `family` | string | BSP family (`nxp`, `ti`, `bbsetup`, ...), or the placeholder `"unknown"` when a container row's build directory could not be recovered |
| `machine` | string | The resolved MACHINE, or the placeholder `"unknown"` under the same condition as `family` |
| `elapsed_seconds` | integer | Seconds since the run started, or the placeholder `0` when the start time could not be determined |

`family` and `machine` fall back to `"unknown"` specifically for a
container-mode row whose `/work` bind-mount source can't be resolved (the
runtime inspect call failed or timed out), or whose recovered mount source
holds no run record matching that container's run id. `elapsed_seconds`
falls back to `0` when the run directory is unavailable or its name doesn't
parse as a timestamp. An empty result emits `[]`.

```bash
$ bakar ps --json
[{"run_id": "20260701-090000-aaa", "mode": "host", "family": "nxp", "machine": "imx8mp-var-dart", "elapsed_seconds": 750}]
```

## Examples

```bash
# List every live build on this host, from any directory
bakar ps

# Machine-readable form, e.g. for scripting bakar stop --run <id>
bakar ps --json

# Find the run id of a stuck build, then stop it by id
bakar ps
bakar stop --run 20260701-090000-aaa
```

## See also

- [stop.md](stop.md) - stop a build, including the `--run <id>` flag this command's output feeds
- [monitor.md](monitor.md) - live watch of a single running build (cluster load, dist stats, task progress) rather than a host-wide listing
- [triage.md](triage.md) - post-mortem a build that failed or was interrupted
