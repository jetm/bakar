# bakar pytest time profile

Baseline and profile of the bakar test suite, taken 2026-11-17 (UTC-6) before any
change to tests, config or source. Every figure below comes from the raw
measurement files, quoted as recorded and not estimated.

- Host: `model name : AMD Ryzen 9 9950X 16-Core Processor`, 32 threads (`nproc`).
- Collected: 8,351 tests (`testcases=8351` in `serial-default.analysis.txt`).
- Every wall number is indicative only. The 1-minute load before the timed runs
  ranged from 7.88 to 25.56, so the host was never idle and each run shared it
  with other work. The load before each run is in the tables.
- The raw logs stay outside the repository, under
  `/var/tmp/claude-code/bakar/2026-11-17-pytest-runtime/`, which is removed after
  30 days.

## Baseline (2026-11-17)

Each run recorded the first field of `/proc/loadavg` before it started, then ran
the command under `/usr/bin/time -f 'TIME wall=%e user=%U sys=%S'` from the
primary checkout. All rows are from `times.txt`; user+sys is the sum of the two
recorded fields.

### Serial runs

| run | command (after `uv run pytest`) | load before | wall s | user s | sys s | user+sys s |
|-----|---------------------------------|-------------|--------|--------|-------|------------|
| serial-default | `-p no:randomly --durations=0 --junitxml=...` (default `addopts`, coverage on) | 16.46 | 199.00 | 105.64 | 7.62 | 113.26 |
| serial-nocov | `-p no:randomly --no-cov --durations=0 --junitxml=...` | 7.88 | 204.75 | 108.50 | 8.15 | 116.65 |
| collect-warm | `--collect-only -q --no-cov -p no:randomly` (bytecode warm-up) | 18.30 | 8.67 | 8.43 | 0.14 | 8.57 |
| collect | `--collect-only -q --no-cov -p no:randomly` | 17.02 | 2.80 | 2.71 | 0.08 | 2.79 |

- Serial default: 199.00 s wall, 105.64 s user plus 7.62 s sys, at load 16.46.
- `--no-cov`: 204.75 s wall, 108.50 s user plus 8.15 s sys, at load 7.88.
  Coverage costs nothing measurable here: the no-coverage run is not faster, and
  the coverage core is already SysMonitor.
- Collection alone takes 2.80 s in one process (the first, cache-warming run took
  8.67 s).
- The sum of per-case times in `serial-default.analysis.txt` is 189.15 s over
  8,351 test cases.

### Parallel sweep

The runs used the configured `addopts` (coverage on) with `-q`. `x16-nomatch`
used `--no-cov -k zzzznomatch`, so its workers collect everything and run
nothing.

| run | command (after `uv run pytest`) | load before | rc | wall s | user s | sys s | user+sys s | result |
|-----|---------------------------------|-------------|----|--------|--------|-------|------------|--------|
| x16-ws | `-n 16 --dist=worksteal -q` | 15.68 | 1 | 24.34 | 215.87 | 12.43 | 228.30 | 1 failed, 8338 passed, 12 skipped |
| x16-ws-2 | `-n 16 --dist=worksteal -q` | 22.23 | 1 | 23.56 | 180.96 | 10.79 | 191.75 | 1 failed, 8338 passed, 12 skipped |
| x16-load | `-n 16 --dist=load -q` | 16.82 | 1 | 38.57 | 223.30 | 13.75 | 237.05 | 1 failed, 8338 passed, 12 skipped |
| x8-ws | `-n 8 --dist=worksteal -q` | 25.56 | 0 | 39.45 | 180.69 | 11.93 | 192.62 | 8339 passed, 12 skipped |
| x32-ws | `-n 32 --dist=worksteal -q` | 22.37 | 1 | 20.73 | 339.55 | 18.19 | 357.74 | 1 failed, 8338 passed, 12 skipped |
| x16-nomatch | `-n 16 -q -k zzzznomatch --no-cov` | 15.04 | 5 | 5.88 | 61.73 | 1.76 | 63.49 | no tests ran |

- 16 workers running nothing cost 5.88 s wall and 63.49 CPU-s (user 61.73 plus
  sys 1.76): the fixed cost of starting 16 workers that each collect the suite.
- The failing test in the four runs that failed (`x16-ws`, `x16-ws-2`,
  `x16-load`, `x32-ws`) is the routing-fallback test
  `tests/test_workspace_routing.py::test_routing_error_falls_back_for_read_only_commands`.
  The only parallel run that passed fully is `x8-ws`.
- The parallel wall times span 20.73 s to 39.45 s while the load before them
  spans 15.68 to 25.56, so the ranking between worker counts and schedulers is
  not established by these single runs.

## Census

From `census.json` (one entry per test, 2,135 tests recorded):

- `time.sleep` totals 59.43 s.
- 1,339 command-tree rebuilds, costing 44.60 s.
- About 8,500 direct process spawns (8,501), 7,173 of them `git`.

## Tests at or above 0.5 s

From `targets-0.5.txt`: 76 tests take 0.5 s or more, summing to 69.75 s across
30 files. The three largest files are `tests/test_pin_probe.py` (8.03 s),
`tests/test_e2e_cli.py` (6.44 s) and `tests/test_feed_mirror_download.py`
(5.95 s). The single slowest test is
`tests/test_cli_stop.py::test_no_run_host_wide_fallback_peer_held_root_is_diagnosed_not_dropped`
at 5.00 s.

## Levers

### Command-tree reuse

`tests/conftest.py` registers a `bakar_click_cache` plugin that wraps
`typer.testing._get_command` and returns a stored Click tree only for the same
app object (it keeps a reference to the app, so ids are not recycled). No test
changes an app's commands after invoking it (`rg -F -e registered_commands
-e add_typer -e 'app.command(' tests/` finds only the read-only
`_registered_commands` helper in `tests/test_global_container_flag.py`), so the
cache key is the app alone. Control runs use `-p no:bakar_click_cache`.

Seeds 101, 202 and 303; single-process runs use `-n 0`, parallel runs use the
default xdist options. Every run: 8,342 passed, 13 skipped.

| Run | load before | rc | wall (s) | user+sys (s) | result |
|-----|-------------|----|----------|--------------|--------|
| cc-ctl-101 | 1.52 | 0 | 155.16 | 108.58 | 8342 passed, 13 skipped |
| cc-lev-101 | 1.01 | 0 | 108.27 | 61.71 | 8342 passed, 13 skipped |
| cc-ctl-202 | 1.30 | 0 | 155.43 | 109.22 | 8342 passed, 13 skipped |
| cc-lev-202 | 1.54 | 0 | 109.47 | 63.04 | 8342 passed, 13 skipped |
| cc-ctl-303 | 0.87 | 0 | 156.02 | 109.17 | 8342 passed, 13 skipped |
| cc-lev-303 | 1.63 | 0 | 109.14 | 62.42 | 8342 passed, 13 skipped |
| cc-par-101 | 2.89 | 0 | 15.95 | 133.17 | 8342 passed, 13 skipped |
| cc-par-202 | 4.88 | 0 | 15.77 | 130.00 | 8342 passed, 13 skipped |
| cc-par-303 | 6.09 | 0 | 15.97 | 129.64 | 8342 passed, 13 skipped |

Per-seed wall differences (control minus lever): 101: 46.89 s, 202: 45.96 s,
303: 46.88 s. Median: 46.88 s, above the 10 s threshold; all six cache-on runs
have rc=0.

decision (command-tree reuse): kept

### Collection GC pause

A pytest plugin wrapped `pytest_collection`: `gc.disable()` before collection,
then `gc.enable()`, `gc.collect()` and `gc.freeze()` in a `finally`. Measured
with three interleaved `--collect-only -q -n 0 --no-cov` pairs, the control run
passing `-p no:bakar_harness_gc`, after `wait_idle.sh 540`.

| run | load before | rc | wall (s) |
|-----|-------------|----|----------|
| gc-ctl-1 | 2.47 | 0 | 1.74 |
| gc-lev-1 | 2.47 | 0 | 1.27 |
| gc-ctl-2 | 2.47 | 0 | 1.73 |
| gc-lev-2 | 2.35 | 0 | 1.27 |
| gc-ctl-3 | 2.35 | 0 | 1.74 |
| gc-lev-3 | 2.35 | 0 | 1.27 |

Per-pair wall differences (control minus lever): 0.47 s, 0.46 s, 0.47 s.
Median: 0.47 s, below the 0.5 s threshold; all six runs have rc=0. The plugin,
its registration and `tests/test_harness_gc.py` were removed.

decision (collection GC pause): dropped

### Git fixture environment

A scratch probe (outside the repo) stamped every `git` child at `Popen`
construction and summed the elapsed wall time when the child was reaped, per
test node, over one serial run (`-n 0 -p no:randomly --no-cov`, 8342 passed,
rc=0).

The probe saw 7,177 git spawns against 7,173 in the census, so it missed
none. Total git time: 6.05 s of a 99.83 s run, under the 10 s threshold.

Five modules with the most git time:

| Module | Git seconds | Git spawns |
|--------|-------------|------------|
| tests/test_layer_delivery.py | 0.77 | 1052 |
| tests/test_native_provenance.py | 0.67 | 970 |
| tests/test_pin_sync.py | 0.53 | 411 |
| tests/test_layer_repo_e2e.py | 0.47 | 546 |
| tests/test_cli_sync.py | 0.38 | 375 |

No re-measurement: `tests/conftest.py` was not changed, so there was nothing
to compare. The only timed run had rc=0.

decision (git fixture environment): unchanged
