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

## Remaining time

A fresh single-process profile of the finished suite, taken 2026-11-17 from the
worktree after `bash wait_idle.sh 540` reported `idle: load=0.89 after 0s`. The
host was idle, so these wall numbers are not indicative-only like the baseline.

### Single-process run

Command (after `uv run pytest`): `-p mktemp_time -n 0 -p no:randomly --no-cov -q
--junitxml=...`. Real exit status from the `rc=` field of `times.txt`: `rc=0`
(8342 passed, 13 skipped, 10 warnings).

| run | load before | rc | wall s | user s | sys s | user+sys s |
|-----|-------------|----|--------|--------|-------|------------|
| baseline serial-default | 16.46 | 0 | 199.00 | 105.64 | 7.62 | 113.26 |
| after-serial | 0.82 | 0 | 102.10 | 49.21 | 6.27 | 55.48 |

Single-process wall is 102.10 s against the 199.00 s baseline, and CPU
(user+sys) is 55.48 s against 113.26 s.

### Time buckets

From `buckets.py` over `after-serial.xml` (8,355 test cases, 97.18 s summed):

| bucket (s) | tests | summed s |
|------------|-------|----------|
| >=0.0 | 7231 | 5.44 |
| >=0.005 | 460 | 4.87 |
| >=0.02 | 167 | 5.10 |
| >=0.05 | 201 | 11.67 |
| >=0.1 | 145 | 16.06 |
| >=0.2 | 122 | 32.30 |
| >=0.5 | 24 | 14.87 |
| >=1.0 | 4 | 4.62 |
| >=2.0 | 1 | 2.24 |

Each bucket holds tests from its lower bound up to the next bound. 68 tests
within about 3% above a round wait constant (0.25, 0.3, 0.5, 1.0, 1.5, 2.0, 3.0,
5.0 s) sum to 24.45 s.

### Tests still at or above 0.5 s

`targets.py` at threshold 0.5: 29 tests, 21.74 s, in 15 files (76 tests and
69.75 s in the baseline). Every one has a class:

| File | Tests | Seconds | Class and reason |
|------|-------|---------|------------------|
| tests/test_e2e_cli.py | 9 | 6.38 | deliberately real: drives the installed binary by design |
| tests/test_layer_delivery_remote.py | 3 | 2.25 | work-bound: bash and flock child processes on real git repos |
| tests/test_doctor_cache_mounts.py | 1 | 2.24 | not targeted: no test-visible knob; bare `time.sleep(0.05)` polls at src/bakar/mounts.py:153 (the `_run_bounded` loop) |
| tests/test_layer_repo_e2e.py | 4 | 2.13 | not targeted: end-to-end layer-repository scenarios with git work, no knob |
| tests/test_cli_monitor.py | 1 | 1.56 | work-bound: CPU loop encoding a 3 MB string (multibyte test) |
| tests/test_stress_parse.py | 2 | 1.55 | not targeted: same mounts.py:153 poll, no knob |
| tests/test_pin_probe.py | 1 | 0.80 | wait left: the test sets a 0.65 s drain window and a 0.15 s timeout, and the descendant sleeps 0.4 s inside it |
| tests/test_uninative_leak_scan.py | 1 | 0.68 | not targeted: post-build test, same mounts.py:153 poll |
| tests/test_kas_build_failure_error.py | 1 | 0.65 | not targeted: kas build driver, no knob |
| tests/test_cli_build.py | 1 | 0.63 | not targeted: build driver, no knob |
| tests/test_reuse_lock.py | 1 | 0.63 | wait left: 0.2 s and 0.1 s sleeps in the test body order the holder and the front end |
| tests/test_kas_build_plain.py | 1 | 0.61 | not targeted: kas build driver, no knob |
| tests/test_eventlog_tail.py | 1 | 0.60 | not targeted: the literal `stop_event.wait(0.2)` in src/bakar/eventlog.py, no knob |
| tests/integration/test_hashserv_real_daemon.py | 1 | 0.51 | deliberately real: tests/integration/ runs real daemons |
| tests/test_cli_build_layer.py | 1 | 0.50 | not targeted: layer build path, no knob |

The 29 tests sum to 21.72 s from the rows (21.74 s from `targets.py`, rounding).

Not-targeted modules that no longer have a test at or above 0.5 s, with this
run's whole-file totals: tests/test_getvar.py 4.31 s (slowest test 0.21 s),
tests/test_layer_repo_kas_guard.py 1.89 s (four tests at 0.43 to 0.48 s),
tests/test_home_isolation.py 0.78 s, tests/test_report_run_resolution.py 0.00 s,
and tests/test_uninative_leak_scan.py::test_leak_via_dt_needed_edge (below 0.5 s;
the file totals 1.14 s). They are not targeted because no test-visible knob
exists: each runs real helper subprocess or import-time work spread over many
short tests.

Group-2 targets left unchanged for want of a knob, with the caller named:

- The stress-parse pair (tests/test_stress_parse.py) and the doctor cache-mounts
  test: bare 0.05 s `time.sleep` polls at src/bakar/mounts.py:153, the
  `_run_bounded` loop.
- The event-log tail test: the literal `stop_event.wait(0.2)` in
  src/bakar/eventlog.py.
- The uninative post-build test: the same mounts.py poll.
- The monitor multibyte test (tests/test_cli_monitor.py): a CPU loop encoding a
  3 MB string.

### Parallel run over 15 s

One parallel default run with `--durations=0 --durations-min=15`: `rc=0`, wall
15.06 s (pytest reported 14.11 s), user 116.97 s, sys 9.61 s, 8342 passed.

Tests over 15 s under 16 workers: none

The log lists no durations at all; its only report line is `(25065 durations <
15s hidden.)`.

### Pytest temporary-directory cost

mktemp time in a single-process run: 0.443 s

That is 13,803 `TempPathFactory.mktemp` calls, below the 5 s level, so it is not
a lever and no code was changed for it.

### Flaky test, found and fixed

tests/test_pin_probe.py::test_run_bash_local_timeout_kills_the_whole_process_group
failed once in 42 full parallel runs (`assert _gone(grandchild)`). It also failed
at the original 1.0 s timeout (3 of 250 trials under suite load, against 5 of
400 at 0.2 s), so the shortened waits did not cause it.

The grandchild was never alive. The test polls `_gone()` and then asserts
`_gone()` again, a second read. `_gone()` counted a missing `/proc` entry or a
zombie (`Z`) as gone but not `X`, and a killed child passes through `Z`, then
`X` (dead, being released), before its entry disappears. A first read on `Z`
followed by a second on `X` made `_gone()` flip from True to False. A scratch
probe that evaluated the old predicate right after the child was seen gone found
10 disagreements in 1,500 trials (0.67%), each showing state `X` in
`/proc/<pid>/stat`; no killed grandchild stayed non-zombie for even 50 ms in
1,500 trials. With `X` counted as dead there were 0 disagreements in 2,500
trials. The same double read is in the interrupt test, so the one helper fix
covers both.

### Next levers

Ranked by seconds in this profile, largest first:

1. The bare `time.sleep(0.05)` poll at src/bakar/mounts.py:153 holds up three
   tests at or above 0.5 s: doctor cache-mounts 2.24 s, stress parse 1.55 s and
   uninative post-build 0.68 s, 4.47 s together. A production-side change for a
   later change.
2. tests/test_cli_monitor.py multibyte test, 1.56 s of CPU encoding a 3 MB
   string; a smaller string that still splits a multibyte character would cut it.
3. The `stop_event.wait(0.2)` in src/bakar/eventlog.py behind the 0.60 s
   event-log tail test: a production-side follow-up for a later change.
4. tests/test_layer_delivery_remote.py, 2.25 s of work-bound child processes in
   three tests; only fewer real moves would shrink it.
5. tests/test_e2e_cli.py, 6.38 s, stays: it drives the installed binary by
   design.

## Result

Measured 2026-11-17 from the worktree, with a base-commit control built from an
archive of 97125fffecf7 plus the worktree's `ARCHITECTURE.md` and `CLAUDE.md`
symlinks (so both trees skip the same 13 tests). Every `rc=` below is the value
from the run's line in `times.txt`.

### Collected ids

`pytest --collect-only -qq -n 0 --no-cov -p no:randomly`, lines containing `::`:

- Base: 8351 ids. Change: 8355 ids (four added tests).

Collected ids missing from the change: 0

### Default runs

| run | load before | rc | wall s | user+sys s | result |
|-----|-------------|----|--------|------------|--------|
| control-default (base, single-process) | 0.58 | 0 | 188.08 | 104.78 | 8338 passed, 13 skipped |
| change-default-1 | 0.64 | 0 | 14.87 | 123.80 | 8342 passed, 13 skipped, coverage 80.0% reached |
| change-default-2 | 2.48 | 0 | 15.10 | 123.46 | 8342 passed, 13 skipped, coverage 80.0% reached |
| change-default-3 | 3.25 | 0 | 14.94 | 124.39 | 8342 passed, 13 skipped, coverage 80.0% reached |
| change-serial-rs (`-n 0`) | 5.47 | 0 | 106.89 | 60.21 | 8342 passed, 13 skipped |

Run 3 started at load 3.25, which is the tail of run 2 and a sibling session on
the shared host; run 1 started under 1. Runs 1 to 3 differ by 0.23 s, so load
did not move the result. Failed runs among the three change runs: 0 of 3.

Median default wall: 14.94 s

The control wall of 188.08 s against the median change wall of 14.94 s is a
12.6x speedup. Against the recorded baseline of 199.00 s wall and 113.26 CPU-s,
the change default run uses 14.94 s wall (13.3x) and 123.8 CPU-s: wall time fell
because the work spreads over workers, and CPU rose by about 9% from worker
start-up and duplicated imports. The single-process change run is 106.89 s wall
and 60.21 CPU-s, from the test-level fixes.

### Skip reasons

SKIPPED lines from `change-serial-rs` and `control-default` (10 each, line
numbers stripped, sorted) compared with `diff`: no difference.

Skip reasons identical: yes

### Interrupt-ignored start

`bash -c "trap '' INT; exec uv run pytest -q"`. The first run (change-int-ignored)
gave rc=1: 1 failed, 8341 passed, 13 skipped. The failing test was
`tests/test_avocado_cli.py::test_keyboard_interrupt_terminates_the_child_group_and_reraises`,
which hit the 30 s pytest-timeout in `_run_one`. It fails identically on the
untouched base under an ignored SIGINT (int-one-control, rc=1, 30.85 s), because
Python installs no KeyboardInterrupt handler when SIGINT starts ignored. Commit
5bdc2bbd fixed the test so it installs the default SIGINT handler itself.

The repeat run, change-int-ignored-2, started at load 0.24: rc=0, wall 14.68 s,
8342 passed, 13 skipped, and its log carries `Required test coverage of 80.0%
reached`.

Interrupt-ignored run: passed
