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
  linux-imx: 0 hits, 1 misses, 100.0% miss
  busybox: 12 hits, 0 misses, 0.0% miss
```

When no sstate data was captured for the run, a single message line replaces
the per-recipe listing (e.g. `no sstate data found for run`).

### timing

Per-task duration for the top-N slowest tasks (`--top`, default 10), each
annotated with the recipe's historical baseline mean when one exists, plus a
critical-path note:

```text
timing:
  linux-imx:do_compile: 812.3s (baseline mean 790.4s)
  core-image-minimal:do_rootfs: 214.1s
critical path:
  critical-path unavailable
```

The critical-path section always renders as unavailable from this command:
computing it needs a live `bitbake -g <recipe>` invocation (see
`bakar graph`), and which recipe to graph isn't knowable from a bare run
directory. `insights.py` never supplies a `dependency_source` callable, so
`CriticalPath`'s default `note` field - the literal string
`critical-path unavailable` - is always what prints. The duration and
top-N-slowest sections still render fully from the run's persisted event
artifact.

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
`not resource-pressured` or a message explaining the missing data).

### disk

Net disk growth in bytes for the run, any captured `DiskFull` event
surfaced separately, and an optional threshold warning:

```text
disk:
  growth: 5368709120 bytes
  [yellow]disk growth 5368709120 bytes exceeds threshold 5000000000 bytes[/]
  disk full: {'time': 1735689602.0, 'path': '/bsp/nxp/build/tmp', 'message': 'Disk full on /bsp/nxp/build/tmp: only 1024 KiB free'}
```

The threshold warning appears only when `--growth-threshold` is given and
exceeded. `disk full:` lines appear only when the run recorded a `DiskFull`
event.

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
