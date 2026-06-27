# bakar monitor

One-view watch for a running build: sccache-dist cluster load, the in-container
build daemon's cache/dist stats, and bitbake task progress (done/running/failed,
elapsed, recent failures) - aggregated from signals that otherwise live in three
separate places (`cluster-info`, a `kas.log` tail, and the build-server journal).

## Synopsis

```text
bakar monitor [KAS_YAML] [OPTIONS]
```

Resolve the run the same way `bakar log` does: the latest run for the workspace,
or a specific one with `--run`. The positional `KAS_YAML` selects a BYO build
(runs are read next to the YAML); omit it for a manifest workspace.

> Run it from the build workspace (or pass `--workspace`). Like the other
> run-scoped commands, `monitor` resolves the run dir relative to the workspace,
> so invoking it from an unrelated directory reports "no runs yet".

## Options

| Flag | Description |
|------|-------------|
| `--run` | Run ID (`YYYYMMDD-HHMMSS`). Latest run if omitted |
| `--scheduler` | Scheduler URL override (default: from `--sccache-scheduler` or config) |
| `--interval`, `-n` | Refresh interval in seconds (default: `2.0`) |
| `--once` | Render a single snapshot, then exit (non-watch) |
| `--json` | Emit one JSON snapshot to stdout and exit |
| `--watch` | With `--json`, stream NDJSON - one object per interval |
| `--workspace`, `-w` | Workspace root override |

## Output modes

| Invocation | Output |
|------------|--------|
| `bakar monitor` | Refreshing Rich view on **stderr** until the build finishes, then a final frame |
| `bakar monitor --once` | One Rich frame on stderr, then exit |
| `bakar monitor --json` | One JSON snapshot to **stdout**, then exit |
| `bakar monitor --json --watch` | NDJSON stream to stdout (one compact object per `--interval`), until the build finishes |

Human/Rich output always goes to stderr, so a piped `--json` consumer sees only
the JSON document on stdout.

## JSON snapshot schema

`--json`, `--json --once`, and each `--json --watch` line emit the same document:

```json
{
  "run": "20260626-174304",
  "cluster": {
    "reachable": true,
    "scheduler_url": "http://192.168.8.174:10600",
    "error": null,
    "capacity": {"num_servers": 2, "num_cpus": 64, "in_progress": 45,
                 "servers": [{"id": "...", "num_cpus": 32, "in_progress": 25}]}
  },
  "build_daemon": {"container": "...", "verdict": "DISTRIBUTING",
                   "cache_hits": 6, "cache_misses": 648, "distributed": 4,
                   "dist_errors": 422, "per_node": {"...": 3}, "cache_location": "..."},
  "build": {"outcome": "unknown", "live": true, "started": null, "completed": null,
            "elapsed_seconds": 10795, "tasks_total": 12183, "tasks_done": 12035,
            "tasks_remaining": 148, "tasks_running": 1, "tasks_failed": 0,
            "running": [{"recipe": "...", "task": "do_compile"}],
            "failures": ["...last 5..."]}
}
```

`cluster` and `build_daemon` mirror `bakar cluster-info --json`; `build_daemon`
is `null` when no build container is running. When no run exists the document is
`{"error": "no runs yet; start one with 'bakar build'"}` and the exit code is 1.

`tasks_total`/`tasks_done`/`tasks_remaining` come from the bitbake runqueue
(the same counts as bitbake's "X of Y tasks"); they are `null` until the first
`runQueueTaskStarted` is seen (early parse/setscene), where `tasks_done` falls
back to the executed-task success count. `elapsed_seconds` is derived from the
run directory's `YYYYMMDD-HHMMSS` name, because bitbake's `BuildStarted` event
carries no timestamp (so `started` stays `null`).

## Examples

```bash
# Live refreshing view for the current build (most common, in a terminal)
bakar monitor

# Live view for a BYO build, from the build workspace
bakar --sccache-dist monitor meta-avocado/kas/machine/qemuarm64.yml

# One snapshot to stdout for a script or CI step
bakar --sccache-dist monitor --json --once meta-avocado/kas/machine/qemuarm64.yml

# Stream NDJSON to a CI log (one line per 5s) until the build finishes
bakar --sccache-dist monitor --json --watch -n 5 meta-avocado/kas/machine/qemuarm64.yml

# Monitor a specific past run
bakar monitor --run 20260626-174304
```

## Notes

- Press Ctrl+C to stop the live/watch loop (exits 0).
- The build-daemon probe shells out to `docker` twice with 15s timeouts; it is
  throttled to at most one probe every 3s, so a fast `--interval` cannot stack
  heavy subprocesses.
- Pass `--sccache-dist` (or set `build.sccache_dist`) so the cluster probe knows
  which scheduler to query; without it the `cluster` block reports unreachable.
- `dist_errors` counts distributed compiles that fell back to local. A high count
  under a saturated cluster is expected (compiles overflow to local when both
  nodes are full); it is not by itself a failure signal.

## See also

- [log.md](log.md) - tail a single run log live
- [triage.md](triage.md) - post-mortem the failing recipe/task
- [report.md](report.md) - summarize a completed run
- [sccache-dist.md](sccache-dist.md) - distributed compilation setup and `cluster-info`
