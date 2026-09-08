# bakar cluster-info

Query the sccache-dist scheduler and print its live capacity: how many build
servers are registered, how many CPUs they add up to, and how many jobs are in
progress right now. When a build container is up, the in-container sccache
daemon's cache/dist stats are printed alongside it.

## Synopsis

```text
bakar cluster-info [OPTIONS]
```

No workspace is needed - the command talks to the scheduler, not to a build
tree, so it runs from anywhere.

## Options

| Flag | Description |
|------|-------------|
| `--scheduler` | Scheduler URL override |
| `--json` | Emit a JSON document with the scheduler capacity |

## Scheduler URL resolution

The scheduler URL is taken from the first of these that is set:

1. `--scheduler` on this command.
2. The global `--sccache-scheduler` option (`bakar --sccache-scheduler URL cluster-info`).
3. `sccache_scheduler_url` in `~/.config/bakar/config.toml`.

When none is set, the probe runs `sccache --dist-status` with no override and
sccache uses whatever scheduler its own config names; the human output then
prints `scheduler: (from sccache config)`.

## Output

```text
sccache-dist cluster:
  scheduler: http://192.168.8.174:10600
  build servers: 2
  cpus: 64
  jobs in progress: 45
```

Per-node detail is not available from the upstream scheduler - it exposes the
aggregate only. When a forked scheduler does return a per-server array, each
entry is printed under a `nodes:` list as `<id> - <cpus> cpus, <n> job(s)`.

When a bakar build container is running, a `build daemon` block follows with
cache hits/misses (overall and per language), distributed vs local compile
counts, distributed-error count, per-node distribution counts, the cache
location, and a `verdict` of `DISTRIBUTING`, `LOCAL-ONLY`, or another state.

## JSON output

`--json` emits one document to stdout:

```json
{
  "reachable": true,
  "scheduler_url": "http://192.168.8.174:10600",
  "error": null,
  "capacity": {"num_servers": 2, "num_cpus": 64, "in_progress": 45, "servers": []},
  "build_daemon": null
}
```

`build_daemon` is `null` when no build container is running. `capacity` is
`null` and `error` carries the reason when the scheduler could not be reached.
The same `cluster` and `build_daemon` shapes appear inside a
`bakar monitor --json` snapshot.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | The scheduler answered and capacity was parsed |
| 1 | The scheduler is unreachable, returned no capacity, or `sccache` is not on `PATH` |

`--json` still prints the document (with `reachable: false` and the reason in
`error`) before exiting 1, so a script can read the reason rather than only the
status.

Common `error` values, all reported without raising:

- `sccache binary not found on PATH`
- `sccache --dist-status exited <N>: <detail>`
- `scheduler unreachable or returned no capacity`

## Examples

```bash
# Live capacity from the configured scheduler
bakar cluster-info

# Point at a different scheduler for one call
bakar cluster-info --scheduler http://192.168.8.174:10600

# Machine-readable, for a CI gate
bakar cluster-info --json
```

## See also

- [sccache-dist.md](sccache-dist.md) - distributed compilation setup
- [monitor.md](monitor.md) - live build view that embeds this same cluster block
- [sched-triage.md](sched-triage.md) - post-hoc scheduler/client log triage
