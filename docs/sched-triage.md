# bakar sched-triage

Aggregate the sccache-dist scheduler journal and the client error log into one
cluster-utilisation triage report, instead of running `journalctl` and `grep` by
hand across two machines. Read-only and post-hoc: it explains a finished (or
in-flight) build's distribution behaviour rather than watching it live.

## Synopsis

```text
bakar sched-triage [OPTIONS]
```

No workspace is needed - the inputs are a systemd journal and a log file path.

## Options

| Flag | Description |
|------|-------------|
| `--since` | `journalctl --since` window for the scheduler unit (default: `1 hour ago`) |
| `--client-log` | Client `SCCACHE_ERROR_LOG` path (default: `$SCCACHE_ERROR_LOG`) |
| `--unit` | Scheduler systemd unit (default: `sccache-scheduler.service`) |
| `--events` | `bitbake-events.json` for the per-phase (`do_compile` supply) util join |
| `--json` | Emit the triage report as one JSON object |

## Inputs

Two sources, each independently optional - a section whose source is empty
reports zeros rather than failing:

- **Scheduler journal.** `journalctl -u <unit> --since <since> -o short-unix`.
  The `dist-alloc` and `dist-status` lines it parses only exist when the R0
  scheduler drop-in is in place (`SCCACHE_LOG=info`). Any failure to run
  `journalctl` yields an empty journal.
- **Client log.** The `SCCACHE_ERROR_LOG` file written by the sccache client
  during the build, supplying per-compile timers, local fallbacks, and remote
  rustc errors. It is streamed line by line, so a multi-hundred-MB log does not
  have to fit in memory. An unreadable path is treated as an empty log.

`--events` adds a third, optional input: a run's `bitbake-events.json`, from
which `do_compile` start/completion spans are read to bucket scheduler polls by
how many compiles were actually in flight. A missing file, bad JSON, or an
unexpected shape yields no intervals and the per-supply section is skipped.

## Report sections

### scheduler routing (W1)

Total allocations, misroutes (the scheduler chose a busier server over a
less-loaded one) with their percentage, and idle skips (a zero-job server that
was skipped). Truncated candidate lines - where a `load == 0` break cut the
candidate list short - are excluded from the rate and reported separately.
High-load misroutes are called out on their own as the actionable rate: a wrong
choice made while both nodes were already loaded. A per-node breakdown of
chosen servers follows when present.

### cluster saturation

Poll count, core ceiling and admission ceiling, mean utilisation and mean
in-flight job count, plus the share of polls that were idle, below 1/8 of the
ceiling, and near-saturated against the admission ceiling.

When a poll series exists, a time-weighted utilisation is also printed with its
median cadence and maximum gap - a large gap means an "idle" stretch may just
be unobserved.

With `--events`, a per-supply breakdown buckets polls by live `do_compile`
count (`no compiles`, `1-7`, `>=8`) and prints each bucket's utilisation and
per-node jobs/cores ratio. In the high-supply bucket, a node far below its
jobs/cores share indicates a feed bottleneck.

### client compiles (W2)

Distributed job count, per-node breakdown, and mean per-job timings split into
`put_tc` and `run+fetch` (plus `preprocess` when the client was built with the
W2 timer - otherwise the report says so rather than reporting a zero).
Preprocess concurrency p95/max is reported when the client logged it. Local
compiles that were never eligible for distribution (configure conftests) are
counted separately from fallbacks, and each fallback reason is listed with its
count.

### rust distribution (W3)

Remote rustc error codes with counts, or a line stating no remote rustc errors
appeared in the client log.

## JSON output

`--json` emits one object with these top-level keys:

| Key | Contents |
|-----|----------|
| `since` | The window passed to `journalctl` |
| `client_log` | Resolved client log path, or `null` |
| `routing` | Allocation stats: `total`, `misroutes`, `idle_skips`, `truncated`, `per_node_chosen`, and the per-bucket totals |
| `saturation` | `samples`, `ceiling`, `admission_ceiling`, `mean_inflight`, `mean_util_pct`, `idle_pct`, `under_eighth_pct`, `near_sat_pct` |
| `time_weighted` | `mean_util_pct`, `median_cadence_s`, `max_gap_s` |
| `client` | `jobs`, `per_node_jobs`, the mean timers, `not_eligible`, `fallback_reasons`, `rust_error_codes`, preprocess concurrency |
| `conditioned` | Present only with `--events`: the `idle`/`low`/`high` supply buckets |

## Exit codes

`bakar sched-triage` always exits 0. Empty inputs are reported as zeros in the
output, not as an error status - check the section counts (`routing.total`,
`saturation.samples`, `client.jobs`) to tell "nothing happened" from "nothing
was read".

## Examples

```bash
# Last hour of scheduler activity plus the client log from $SCCACHE_ERROR_LOG
bakar sched-triage

# A specific past window and an explicit client log
bakar sched-triage --since "2026-07-02 09:00" --client-log /var/log/sccache-client.log

# Join scheduler polls against a run's do_compile timeline
bakar sched-triage --events build/runs/20260702-091500/bitbake-events.json

# Machine-readable, for archiving next to the run
bakar sched-triage --json > triage.json
```

## See also

- [cluster-info.md](cluster-info.md) - live scheduler capacity, not a log post-mortem
- [monitor.md](monitor.md) - live one-view watch for a running build
- [sccache-dist.md](sccache-dist.md) - distributed compilation setup
- [triage.md](triage.md) - post-mortem a failed build's recipe/task
