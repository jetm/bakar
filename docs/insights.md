# bakar insights

Render per-recipe/per-task analytics for a completed run: sstate cache
hit/miss breakdown, per-task timing and top-N slowest tasks, PSI
CPU/IO/memory pressure share, and disk-usage growth.

## Synopsis

```text
bakar insights [RUN_ID] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `RUN_ID` | | Run ID (`YYYYMMDD-HHMMSS`). Latest run if omitted |
| `--manifest` | `-f` | Manifest filename used to dispatch BSP family |
| `--workspace` | `-w` | Workspace root override |
| `--sstate` | | Show the per-recipe sstate hit/miss report |
| `--timing` | | Show the per-task timing and top-N-slowest report |
| `--pressure` | | Show the PSI CPU/IO/memory pressure report |
| `--disk` | | Show the disk-usage growth report |
| `--top` | | Number of slowest tasks to show in the timing report (default `10`) |
| `--growth-threshold` | | Warn when disk growth exceeds this size (e.g. `5GB`) |

With no `--sstate`/`--timing`/`--pressure`/`--disk` flag, all four sections
render.

## Run selection

Run-dir selection mirrors `bakar report`: an explicit `RUN_ID` argument
selects that run; omitting it selects the latest run under the resolved
workspace's search roots (`nxp/build/runs/`, `ti/build/runs/`,
`build/runs/`, and any `build-*/build/runs/` preset directories, or the
bbsetup workspace's `build/runs/`). `bakar insights` always prints which run
it targeted:

```text
:: insights 20260601-143022
```

so a `--preset` multi-release build's `bakar insights` (no selector) never
silently aggregates across runs - it names exactly the one run it read.

If the named run isn't found, or no runs exist yet under the search roots,
the command prints an error and exits non-zero rather than printing empty
sections.

## Examples

```bash
# All four sections for the most recent run
bakar insights

# All four sections for a specific run
bakar insights 20260601-143022

# Just the sstate breakdown
bakar insights --sstate

# Timing, showing the top 20 slowest tasks
bakar insights --timing --top 20

# Pressure and disk together, warning above 5GB growth
bakar insights --pressure --disk --growth-threshold 5GB
```

## Output

### sstate

Per-recipe sstate hit/miss counts, sorted by descending misses:

```text
sstate:
  linux-imx-6.12-r0: 0 hits, 1 misses, 100.0% miss
  busybox-1.36.1-r0: 12 hits, 0 misses, 0.0% miss
```

Recipe names are printed exactly as bitbake's event log recorded them - the
full versioned PF (e.g. `busybox-1.36.1-r0`), not the bare package name.

When no sstate data was captured for the run, a single message line replaces
the per-recipe listing (e.g. `no sstate data found for run`).

### timing

Per-task duration for the top-N slowest tasks (`--top`, default 10), each
annotated with the recipe's historical baseline mean when one exists, plus a
critical-path note:

```text
timing:
  linux-imx-6.12-r0:do_compile: 812.3s (baseline mean 790.4s)
  core-image-minimal-1.0-r0:do_rootfs: 214.1s
critical path:
  critical-path unavailable
buildstats join:
  buildstats join 100.0% (433 of 433 executed tasks matched a buildstats record)
cpu floor:
  CPU floor 45.7s = 1461.9 joined CPU seconds / 32 cores
concurrency floor:
  concurrency floor unavailable: the critical path is unavailable, so which
  bound binds cannot be determined
task churn:
  task                          tasks     minflt    majflt   syscalls  GB_wr
  do_compile                       38   26652217       997    4631211   3.00
  do_configure                     36   10192800        62     943052   0.13
```

The critical-path section always renders as unavailable from this command:
computing it needs a live `bitbake -g <recipe>` invocation (see
`bakar graph`), and which recipe to graph isn't knowable from a bare run
directory. `insights.py` never supplies a `dependency_source` callable, so
`CriticalPath`'s default `note` field - the literal string
`critical-path unavailable` - is always what prints. The duration and
top-N-slowest sections still render fully from the run's persisted event
artifact.

The **concurrency floor** is `max(CPU floor, critical path)` and needs both
bounds, so it degrades to a note here for the same reason. That degradation is
deliberate rather than a gap: reporting the CPU floor alone under the
concurrency-floor label would present a throughput bound as a dependency bound,
which is how an 11.9% CPU-only headroom reads as actionable on a build whose
real headroom is 1.1%.

The **buildstats join** gates every CPU-derived figure. When fewer than 95% of
executed tasks carry a buildstats record, no CPU duration appears anywhere in
the output and the note names the achieved rate - a floor computed over a
partial task set is confidently wrong and reads exactly like a correct one.

The capture it joins against is the one correlated with the reported run's own
build window, and the section names that directory whether it publishes or
refuses. A build directory accumulates one capture per run, so taking the newest
would join an older run against a later build's records; consecutive builds of
one target execute a near-identical `(PN, task)` set, so that join clears the
95% gate at close to 100% over the wrong build. A run with no correlatable
capture reports that as its own outcome, distinct from a missing tree and from a
build that recorded nothing.

A record only counts for the exact `PF:task` the run executed. Where the event
log and the buildstats tree disagree by a revision suffix alone, and exactly one
candidate record carries the version-stripped name, that record still joins;
where two versions of a recipe are present and neither matches exactly, the task
counts as unjoined and lowers the rate rather than crediting a version the build
never ran.

The **CPU floor** divides joined CPU seconds by the core count recorded on the
build host at capture time, not by the analysing host's. A run captured before
that field existed reports the floor as unavailable rather than substituting the
local core count.

The **task churn** columns aggregate `minflt`, `majflt`, `syscalls` and bytes
written per task type. The minor-to-major fault ratio is what separates
process-churn-bound work from I/O-bound work: on a real capture the columns rank
21 task types across 3.68 orders of magnitude. Note the fault counters sum the
task's own rusage and its children's - the child typically carries the large
majority - while the IO counters are self-only, because bitbake reads them from
`/proc/<pid>/io` for the task process alone.

### pressure

PSI CPU/IO/memory time-share percentages plus a plain-language verdict
naming the dominant pressure type:

```text
pressure:
  cpu: 12.4%
  io: 61.8%
  memory: 3.1%
  verdict: I/O pressure dominated this build (61.8% avg10 time-share)
```

When no PSI samples were captured, the verdict alone renders (e.g.
`not resource-pressured` or a message explaining the missing data). A
dimension with zero usable readings (e.g. `read_psi_avg10` failing for one
resource on a given host) is omitted from the percentages entirely rather
than shown as `0.0%`, so a measurement gap is never misread as confirmed
zero pressure on that dimension.

### disk

Net disk growth in bytes for the run, any captured `DiskFull` event
surfaced separately, and an optional threshold warning:

```text
disk:
  growth: 5368709120 bytes
  disk growth 5368709120 bytes exceeds threshold 5000000000 bytes
  disk full: {'dev': '/dev/mmcblk0p2', 'type': 'ext4', 'free_bytes': 1024, 'mountpoint': '/bsp/nxp/build/tmp'}
```

The threshold warning line renders in yellow and the `disk full:` label in
red (Rich markup - the example above shows the plain text a terminal
without color would print). The threshold warning appears only when
`--growth-threshold` is given and exceeded. `disk full:` lines appear only
when the run recorded a `DiskFull` event, and reflect bitbake's real
`bb.event.DiskFull` fields (`dev`/`type`/`free_bytes`/`mountpoint` - it
carries no timestamp or message text of its own).

## Notes

- All output goes to stderr (consistent with `bakar report`); there is no
  `--json` mode for `insights`.
- `--growth-threshold` accepts a bare byte count or a size with a binary
  (1024-based) suffix: `b`, `kb`/`k`, `mb`/`m`, `gb`/`g`, `tb`/`t`
  (case-insensitive), e.g. `5GB` or `512000000`.
- Each section degrades independently: a run missing PSI samples still
  renders sstate/timing/disk sections normally.

## See also

- [report.md](report.md) - success-path run summary (status, duration, image size, layers)
- [graph.md](graph.md) - live `bitbake -g` dependency graph analysis, including the critical-path computation `insights --timing` cannot do
- [log.md](log.md) - tail the raw kas.log or events.jsonl for a run
- [monitor.md](monitor.md) - live one-view watch of a running build
