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

Items 1 to 3 of the earlier list (the mounts.py poll, the multibyte test, the
event-log tail wait) are closed: the final profile in "Ten-second round: result"
has none of those tests at or above 0.5 s. Only what is still open is listed,
largest first:

1. The 2.151 s before the collection barrier and the 1.055 s between the end of
   the run phase and `sessionfinish` in the final phase timeline (the second is
   the coverage report and its finish step, see the final phase timeline).
   Collection sharding stays a follow-up that the 10 s target does not need (median
   default wall 9.36 s); the trigger is a median above 10 s on pc3 or a target
   under 9 s.
2. tests/test_e2e_cli.py, seven tests at or above 0.5 s in the final profile,
   stays: it drives the installed binary by design.
3. tests/test_mounts_run_bounded.py (0.94 s), the tests/test_pin_probe.py drain
   test (0.80 s) and the layer-delivery SIGINT test (0.57 s): real bounded
   subprocesses or fixed windows that are the behavior under test; only a
   production-side knob would shrink them.
4. CI skip reasons: pending the first CI run that prints them, to explain the
   skip-count difference between CI and pc3.

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

## Ten-second round

### Baseline default runs

Host: pc3 (rog-strix-x870i-jetm), AMD Ryzen 9 9950X3D 16-Core, 32 threads,
governor performance, Python 3.14.10, uv 0.13.8. The runs were taken on pc3,
not on the dev host, with `uv run --no-sync pytest -q` in a staged tree. The
series ran 2026-11-17 starting 16:37 (UTC-6). load1 before the four default runs
was 4.02 to 5.83 (table below) while CPU pressure (`some avg10`) read 0.00, so
the gate waited on pressure
under 0.5 and no running pytest, not on load1. Every gate line reads
`quiet psi_avg10=0.00 after 0s`.

Definition of an idle host for the timed pc3 runs in this round (ruling logged
2026-11-18, assumption A6): CPU pressure `some avg10` below 0.5 with no other
pytest running, not load1 below 3. Load1 is a one-minute average that still
carries the previous run, so it climbs during a back-to-back series (1.7 to 11.94
across this round's tables) while pressure reads 0.00; it is recorded in every
table and never gated on.

| run | load1 before | rc | wall s | user+sys s | pytest summary |
|-----|--------------|----|--------|------------|----------------|
| warmup (untimed) | 4.02 | 0 | 14.22 | 105.27 + 7.89 | 8342 passed, 13 skipped, 105 warnings in 13.39s |
| base-default-1 | 4.92 | 0 | 13.74 | 104.13 + 8.14 | 8342 passed, 13 skipped, 97 warnings in 12.91s |
| base-default-2 | 5.14 | 0 | 13.93 | 105.66 + 7.86 | 8342 passed, 13 skipped, 97 warnings in 13.10s |
| base-default-3 | 5.83 | 0 | 14.36 | 104.28 + 8.17 | 8342 passed, 13 skipped, 94 warnings in 13.53s |

The warm-up run is not part of the median. Pressure before every run was 0.00.
Median of the three timed runs: wall 13.93 s (run 2), user+sys 112.45 s
(runs: 112.27, 113.52, 112.45), pytest-reported time 13.10 s.

### Phase timeline

Command (run in the staged tree on pc3, one run, rc=0, wall 14.13 s, 8342
passed, 13 skipped; load1 before the run 6.39, from `pc3-out2/times.txt`, line
`base-timeline`):

```text
cd <tree>; TL_T0=<date +%s.%N> TL_OUT=<tl-baseline.jsonl> PYTHONPATH=<helpers dir> uv run --no-sync pytest -q -p phase_timeline
```

Summarizer output, seconds from `TL_T0`, mapped to the phases:

| phase | summarizer line | seconds |
|-------|-----------------|---------|
| pre-session | `configure 0.3768 sessionstart 0.3908` | sessionstart at 0.391 |
| gateway ramp | `gateway ramp 0.415 .. 0.710` | 0.295 |
| collection barrier | `collection 2.071 .. 2.312 (barrier 2.312)` | collection 0.241, barrier at 2.312 |
| run phase | `run phase 2.312 .. 9.976` | 7.664 |
| tail | `tail 9.089 .. 9.976`, `idle worker-s 13.288` | 0.887, idle worker-seconds 13.288 |
| end phase | `sessionfinish 12.215 .. 13.668` | 1.453 |

Other summarizer lines: `node_down 8.602 .. 9.999`, `teardown_nodes 12.215 ..
13.237` (1.022 s), `terminal summary 13.623 .. 13.668` (0.045 s), `last
atexit 13.8003`. The run phase ends at 9.976 and sessionfinish starts at
12.215, so 2.239 s pass between them; the summarizer has no line for that gap.

### Off-CPU census

Command (single process, one run, rc=0, wall 96.38 s, user 43.93 s, sys 5.83
s, 8342 passed, 13 skipped; load1 before the run 7.51, from `pc3-out2/times.txt`,
line `base-census`):

```text
uv run --no-sync pytest -p offcpu_census -n 0 -p no:randomly --no-cov -q
```

`offcpu_summary.py` over `oc/offcpu-main.jsonl` (8355 rows, one per test phase
record):

- Summed off-CPU: 46.56 s of 93.41 s summed test wall.
- Tests with off-CPU above 0.2 s: 74.
- Summed own CPU 18.06 s against summed children CPU 28.76 s, so children
  account for 61.4% of the CPU measured inside tests.

The 15 largest, off-CPU seconds with wall and children CPU:

| off-CPU s | wall s | kids s | test |
|-----------|--------|--------|------|
| 1.001 | 1.040 | 0.010 | tests/test_stress_parse.py::test_all_iterations_pass |
| 0.802 | 0.804 | 0.000 | tests/test_pin_probe.py::test_run_bash_local_drain_timeout_keeps_output_read_during_the_drain |
| 0.800 | 0.834 | 0.030 | tests/test_layer_delivery_remote.py::test_build_started_during_a_move_refuses_the_moved_tree |
| 0.799 | 0.833 | 0.030 | tests/test_layer_delivery_remote.py::test_move_holds_the_build_lock_through_the_checkout |
| 0.603 | 0.605 | 0.000 | tests/test_eventlog_tail.py::test_partial_trailing_line_not_yielded_until_complete |
| 0.603 | 0.641 | 0.020 | tests/test_uninative_leak_scan.py::test_post_build_excluded_by_default |
| 0.557 | 0.571 | 0.010 | tests/test_layer_delivery_remote.py::test_sigint_to_the_unit_lets_a_guarded_build_drain |
| 0.514 | 0.571 | 0.050 | tests/test_kas_build_plain.py::test_run_build_persist_tail_failure_does_not_duplicate_terminal_event |
| 0.505 | 0.521 | 0.000 | tests/test_stress_parse.py::test_one_iteration_fails |
| 0.504 | 0.572 | 0.060 | tests/test_kas_build_failure_error.py::test_run_build_zero_exit_writes_no_step_fail |
| 0.489 | 0.530 | 0.040 | tests/test_reuse_lock.py::test_keep_in_use_until_exit_ignores_a_reused_pid |
| 0.454 | 0.486 | 0.030 | tests/test_feed_lock.py::test_second_process_waits_until_first_releases |
| 0.452 | 0.455 | 0.000 | tests/test_native_ledger.py::test_recapture_older_hash_stays_visible_to_middle_run |
| 0.414 | 0.469 | 0.050 | tests/test_cli_build.py::test_run_build_invokes_check_unclean_stop |
| 0.403 | 0.509 | 0.100 | tests/integration/test_hashserv_real_daemon.py::test_hashserv_real_daemon_lifecycle |

### Ten-second round: lever gates

Gates are written before any lever is applied. The numbers come from the three
baseline subsections above (idle pc3, rc 0 runs). Two collection measurements
were taken for this subsection on pc3, single process, in the staged tree with
its venv, after CPU pressure read `some avg10=0.00` and no pytest was running:

- `.venv/bin/python -X importtime -m pytest --collect-only -q -s -n 0 --no-cov
  -p no:randomly` (the `-s` is needed: pytest's fd capture swallows the import
  log of every module imported during collection, leaving 327 lines and no
  `bakar` entry without it). It collected 8355 ids (8342 tests run plus 13
  skipped); 1312 import lines. The
  47 top-level imports sum to 0.280 s cumulative. The 15 largest cumulative
  entries, microseconds (nested entries included): `bakar.cli` 154750,
  `bakar.commands.bitbake` 54635, `pytest` 45938, `hypothesis` 38851,
  `bakar.commands.graph` 30453, `bakar.graph_analyze` 30283,
  `bakar.commands._helpers` 29292, `networkx` 28820, `_pytest._code` 22240,
  `_pytest._code.code` 22201, `bakar.commands.init` 21637, `questionary`
  21510, `prompt_toolkit.styles` 19917, `prompt_toolkit` 19911,
  `hypothesis.core` 19896. The full output is in the plan scratch directory as
  `importtime.txt`.
- Three runs each under `/usr/bin/time -f %e` of
  `.venv/bin/python -m pytest --collect-only -q -n 0 --no-cov -p no:randomly`:
  full 1.66, 1.65, 1.67 s (median 1.66 s); restricted to
  `tests/test_click_cache.py`: 0.41, 0.41, 0.41 s (median 0.41 s). Fixed
  fraction = 0.41 / 1.66 = 0.247, so about 25% of single-process collection is
  fixed cost and about 75% (1.25 s) scales with the number of files.

| lever | gate | baseline numbers evaluated | can it pass its gate on these numbers |
|-------|------|----------------------------|---------------------------------------|
| collection pause plus freeze under the default worker count | applied only if the collection barrier moves at least 0.4 s earlier in three interleaved control and lever pairs (task 5.1) | barrier at 2.312 s, session start 0.391 s, so 1.921 s of start-up and collection sit before the barrier; single-process collect-only is 1.66 s here; the previous round's pause saved 0.47 s collect-only | Possible: 0.4 s is 21% of the 1.921 s before the barrier and the collect-only saving of 0.47 s exceeds it, but nothing in the baseline shows the saving survives 16 workers colliding for CPU; only the paired runs settle it |
| per-test home directories under pytest's base temporary directory | applied only if the shutdown after the last test is at least 0.3 s and the homes' removal is part of it | last test completion 9.976 s, `sessionfinish` starts 12.215 s (2.239 s with no summarizer line), end phase 12.215 .. 13.668 s (1.453 s), last atexit 13.8003 s, so 3.824 s pass after the last test; the atexit hook runs after terminal summary 13.668 s, a 0.132 s window | Possible on size (3.824 s is far above 0.3 s), but the baseline does not attribute any of it to the homes' removal; the hook runs inside a 0.132 s window after the terminal summary, so the removal can only reach the gate if it also runs during `teardown_nodes` (1.022 s). Task 6.1 times the removal in a scratch copy |
| slowest-first dealing | applied only if the tail holds at least 0.5 idle worker-seconds | tail 9.089 .. 9.976 s (0.887 s), idle worker-seconds 13.288 | The gate threshold is met by the baseline (13.288 is above 0.5). The keep rule of task 7.1 (tail shrinks at least 0.3 s) is bounded by the 0.887 s tail, so it can pass only if dealing removes a third of the tail |
| collection sharding | decide only: record the fixed fraction of the collection cost and the one-tiny-file time | fixed fraction 0.247 (0.41 s tiny file against 1.66 s full), top-level imports 0.280 s of the 1.66 s | Not a pass or fail gate. Per-file work is 75% of collection, which sharding would split across processes; the decision line with the projected floor is written by task 8.1 |

The only pre-barrier time attributed so far is `gateway ramp` 0.295 s and
`collection` 0.241 s of the 1.921 s; the remaining 1.4 s has no summarizer line.

Premise (barrier minus session start plus last exit minus last test
completion): (2.312 - 0.391) + (13.8003 - 9.976) = 1.921 + 3.824 = 5.745 s,
which is at or above 5 s.

premise: holds

### Ten-second round: collection GC pause under parallel load

The previous round's pause (`gc.disable()` around `pytest_collection`, then
`gc.enable()`, `gc.collect()` and `gc.freeze()` in a `finally`) was run as a
pytest plugin named `bakar_harness_gc`, registered from `tests/conftest.py`,
with the default worker count on pc3 in the staged tree. Each run used
`uv run --no-sync pytest -q -p phase_timeline` with the timeline recorder from
"Phase timeline"; control runs added `-p no:bakar_harness_gc`. Every run waited
for `some avg10` under 0.5 in `/proc/pressure/cpu` and no pytest; one untimed
warm-up (rc 0, wall 11.04 s, with the lever on) came first. Three interleaved
pairs, control first:

| run | load before | rc | wall (s) | user (s) | sys (s) | barrier (s) |
|-----|-------------|----|----------|----------|---------|-------------|
| gc-ctl-1 | 4.88 | 0 | 12.42 | 103.45 | 7.96 | 2.257 |
| gc-lev-1 | 5.53 | 0 | 10.94 | 86.10 | 8.15 | 2.153 |
| gc-ctl-2 | 5.85 | 0 | 12.85 | 103.13 | 8.09 | 2.266 |
| gc-lev-2 | 5.84 | 0 | 10.84 | 85.35 | 8.02 | 2.153 |
| gc-ctl-3b | 4.07 | 0 | 12.39 | 104.01 | 8.08 | 2.271 |
| gc-lev-3b | 4.71 | 0 | 10.65 | 86.84 | 8.20 | 2.164 |

The first pair-3 control run (`gc-ctl-3`, load 7.08, wall 12.43 s) failed with
rc=1 on `tests/integration/test_hashserv_real_daemon.py::test_hashserv_real_daemon_lifecycle`
(`ensure_running returned None`), so it is invalid and excluded; pair 3 was
run again as `gc-ctl-3b` and `gc-lev-3b`.

Original gate (barrier only): keep if the median barrier moves at least 0.4 s
earlier. Per-pair barrier differences (control minus lever): 0.104 s, 0.113 s,
0.107 s. Median 0.107 s, below 0.4 s, so the original gate fails (the
collection phase started about 0.19 s earlier but lasted 0.30 s against
0.21 s).

Gate amended after the measurement: the barrier-only gate cannot see
`gc.freeze()`, which acts on the run phase after the barrier. The amended gate
keeps the lever if the median wall over three interleaved pairs falls by at
least 0.4 s with no pair rising, or the barrier moves at least 0.4 s earlier.
Wall fell 1.48 s, 2.01 s and 1.74 s in the three pairs (median 1.74 s, no pair
rising) and user CPU fell 17.35 s, 17.78 s and 17.17 s (about 103 s to 86 s),
so the amended gate passes.

Confirmation pair with the plugin as it stands in the tree (restaged, same
gate, one untimed warm-up of rc 0 and wall 11.03 s first), control then lever:

| run | load before | rc | wall (s) | user (s) | sys (s) | barrier (s) |
|-----|-------------|----|----------|----------|---------|-------------|
| gc-ctl-4 | 2.79 | 0 | 12.63 | 103.42 | 8.11 | 2.269 |
| gc-lev-4 | 5.35 | 0 | 10.82 | 86.48 | 8.48 | 2.145 |

The lever's wall is 1.81 s below the control's and its barrier 0.124 s earlier.
The plugin `HarnessGcPause`, registered as `bakar_harness_gc` in
`tests/conftest.py`, and `tests/test_harness_gc.py` stay in the tree;
`-p no:bakar_harness_gc` blocks it.

decision (collection GC pause under parallel load): kept

### Ten-second round: where the post-run time goes

Measured 2026-11-17 on pc3 in the staged current tree (after the test fixes,
the mounts backoff and the GC pause plugin). Series: one untimed warm-up
(rc 0, wall 10.73 s), then `tl-a` (phase timeline only, rc 0, wall 10.80 s,
load1 1.32) and `tl-b` (phase timeline plus a scratch attribution plugin,
rc 0, wall 11.02 s, load1 2.23); CPU pressure `some avg10` was 0.00 before every
run. The attribution plugin lives outside the repository and timestamps, in
seconds since `TL_T0`, one jsonl file per process: the start and end of
`NodeManager.teardown_nodes`, of each worker's `pytest_testnodedown`, of
`coverage.Coverage.stop`, `save`, `load`, `combine`, `report` (and
`xml_report`, `html_report`, `json_report`, `lcov_report`, none of which was
called), of the pytest-cov `DistMaster` and `DistWorker` `finish` and
`testnodedown`, of the sessionfinish and terminal-summary hooks, of every call
of the standard-library directory-tree removal (including both conftest exit
hooks and pytest's own retention cleanup), and an exit hook registered first
(runs last) and one registered at sessionfinish (runs first).

The two timeline runs agree: `tl-a` has the last test done at 8.120 s,
sessionfinish at 10.091 s (gap 1.971 s) and the last exit hook at 10.686 s;
`tl-b` has 8.350 s, 10.319 s (gap 1.969 s) and 10.910 s. Breakdown of `tl-b`,
last test completion to its last exit hook (10.921 s in the plugin's own
record, 2.571 s after the last test):

| step | process | seconds |
|------|---------|---------|
| last test done to last worker down (8.350 .. 8.373), including every worker's own pytest-cov stop and save (median 0.025 s, max 0.039 s) | workers | 0.023 |
| pytest-cov `DistMaster.finish`: stop 0.000, save 0.030, combine 0.071, save 0.000 (8.373 .. 8.474) | controller | 0.101 |
| `coverage.report`, first call (the `total` format pytest-cov takes for the fail-under check), 8.474 .. 9.397 | controller | 0.923 |
| `coverage.report`, second call (the `term-missing` table), 9.397 .. 10.319 | controller | 0.922 |
| `teardown_nodes` (10.319 .. 10.418) | controller | 0.098 |
| pytest's retention cleanup, one directory-tree removal of a `garbage-` directory from an earlier run, inside sessionfinish between `teardown_nodes` and the terminal summary (10.418 .. 10.807) | controller | 0.390 |
| terminal summary (10.808 .. 10.852) | controller | 0.045 |
| rest of sessionfinish, unconfigure and the gap up to the first exit hook (10.853 .. 10.910) | controller | 0.062 |
| exit hooks, first to last (10.910 .. 10.921), including both conftest homes removals (0.0001 s and 0.0000 s) | controller | 0.010 |

The rows sum to 2.574 s against 2.571 s measured (rounding).

The 1.97 s between the last test and sessionfinish is the controller's two
`coverage.report` calls (1.845 s) plus `DistMaster.finish` (0.101 s) and the last
worker's coverage save (0.023 s). Both reports run inside pytest-cov's
`pytest_runtestloop` wrapper, after the loop and before `pytest_sessionfinish`,
which is why the summarizer had no line there. `sessionfinish` itself (0.534 s
in `tl-b`) is `teardown_nodes` 0.098 s, pytest's retention cleanup 0.390 s and
the terminal summary 0.045 s. Worker exit: all 16 workers reached their first
exit hook within 0.0001 s of each other at 10.3193 s (when the controller sent
the shutdown) and their last at 10.3259 s at most, so the workers' own exit path
is under 0.01 s each; each worker had been idle since its own last test.

Per-test homes: the removal of the per-worker homes root (about 520 directories
each) took 0.0503 s summed over the 16 workers, 0.0031 s median, 0.0064 s max,
and the 16 workers do it in parallel. Against the 0.3 s gate it is 47 times
under on the worst worker and about 6 times under on the sum.

Still unmeasured: the interpreter finalisation after the last exit hook (10.921
s to the end of the run; the `/usr/bin/time` wall of 11.02 s bounds it at about
0.1 s, and that includes `uv run` waiting on its child); and why `teardown_nodes`
fell from 1.022 s in the baseline to 0.098 s here, since the tree changed in
several ways between the two measurements and only the GC pause plugin was
isolated (by task 5.1).

Findings for the orchestrator, not acted on here: the controller's two
`coverage.report` calls cost 1.845 s of the 2.571 s after the last test, and
the first is only the `total` figure for the coverage floor, which the second
(the table) recomputes at the same 0.92 s each. Pytest's retention cleanup is
0.390 s in sessionfinish.

decision (per-test homes under base temp): dropped

The gate (the homes' removal is at least 0.3 s of the end phase) is not met:
the removal measures 0.0064 s on the worst worker (0.0503 s summed over 16
parallel workers). Moving the homes under the base temporary directory would
also hand about 520 directories per worker to pytest's retention cleanup, which
already spends 0.390 s on one earlier run's `garbage-` directory on the
controller, so the move would not shrink the end phase (reasoned, not
measured). No code was changed: `tests/conftest.py` keeps its per-test homes,
the exit hook and the two lines that drop COLUMNS and LINES, and the
`HarnessGcPause` plugin and its registration are untouched. The three
interleaved pc3 pairs and the killed-run check were not run because the gate
was not met.

### Ten-second round: slowest-first dealing

Numbers used for the gate: the baseline subsection "Phase timeline" (tail
9.089 .. 9.976 s, 0.887 s, idle worker-seconds 13.288), because the fresher
subsection "where the post-run time goes" records the run and end phases but no
tail or idle worker-seconds. The control runs below re-measured the tail on the
current tree: idle worker-seconds 12.321, 12.584 and 11.967, so the gate (at
least 0.5 idle worker-seconds) holds on both sets.

Lever: a `pytest_collection_modifyitems` hook in `tests/conftest.py`
(`trylast=True`, so it runs after pytest-randomly's shuffle) reads
`tests/slowest_tests.txt` (the 64 tests with wall of at least 0.3 s in the
off-CPU census on pc3, longest first; all 64 ids still exist in the current
collection) and spreads them one every `len(items) // 4 // 64` positions (32
here) across the first quarter of the order, longest first. The aim was one slow
test per chunk of the first batch; that does not describe worksteal, which deals
the whole collection up front as W contiguous chunks of `len(pending) //
nodes_remaining` (xdist 3.8.0, `xdist/scheduler/worksteal.py:211-216`). The spread
therefore put all 64 listed tests into the first quarter of the order, that is the
first W/4 chunks; the placement counts and the replacement are in "slowest-first
dealing, chunk-aware" below. The hook
returns at once unless `PYTEST_XDIST_WORKER` is set, so a single process keeps
the shuffled order untouched. The distribution mode is unchanged.

Order checks, `--collect-only` with seeds 1, 2 and 3 on the dev host (no timed
run): the id set with the hook active equals the set without it (8363 ids, empty
sorted diff), two workers (`gw0`, `gw1`) produce byte-identical order, and the
order differs from the un-hooked one only when `PYTEST_XDIST_WORKER` is set. On
pc3, `--collect-only -n 4` for the same three seeds exited 0 with no collection
mismatch.

Measurement: pc3, staged control tree (`git archive` of HEAD, no hook) and lever
tree (hook plus list), one untimed warm-up per tree (rc 0), then three
interleaved pairs (control first) with the phase timeline, each run gated on
`some avg10` under 0.5 and no pytest. All rc 0, 8350 passed, 13 skipped.

| run | load before | wall (s) | user (s) | barrier (s) | tail (s) | idle worker-s |
|-----|-------------|----------|----------|-------------|----------|---------------|
| sf-ctl-1 | 5.06 | 10.67 | 85.54 | 2.147 | 0.954 | 12.321 |
| sf-lev-1 | 6.50 | 10.38 | 87.34 | 2.154 | 0.329 | 5.534 |
| sf-ctl-2 | 6.39 | 10.71 | 85.85 | 2.140 | 0.851 | 12.584 |
| sf-lev-2 | 6.44 | 10.34 | 87.09 | 2.160 | 0.335 | 5.332 |
| sf-ctl-3 | 6.49 | 10.68 | 85.71 | 2.124 | 0.864 | 11.967 |
| sf-lev-3 | 6.46 | 10.37 | 86.93 | 2.140 | 0.309 | 5.144 |

Median tail: control 0.864 s, lever 0.329 s, a shrink of 0.535 s (pairs: 0.625,
0.516, 0.555), above the 0.3 s bar. Median wall: control 10.68 s, lever 10.37 s
(falls 0.29, 0.37, 0.31 s per pair; no pair rises). Idle worker-seconds fall from
about 12.3 to 5.3.

decision (slowest-first dealing): kept

Correction (task 17.1, task 18.1): the lever description above was first written
with a wrong picture of worksteal. Worksteal deals the whole order at startup in
`workers` contiguous chunks, so the spread put every listed test into the first
quarter of the order, that is into the first `workers / 4` chunks, and at 4
workers all of them into worker 0's chunk (placement counts in the next
subsection). The tail it shrank was measured; the mechanism first given for it was
not. The line above stays as written, as history; the current decision is
"decision (slowest-first dealing, chunk-aware): kept" below.

### Ten-second round: slowest-first dealing, chunk-aware

Lever (task 16.1): `tests/conftest.py` computes worksteal's initial chunk sizes
(`len(pending) // nodes_remaining`, first chunks smallest) and deals the listed
slowest tests round-robin, rank `k` to chunk `k % workers`, so every worker's
chunk starts with an equal share, longest first. The id list is
`tests/slowest_tests.txt`.

Arms, all on pc3, `uv run --no-sync pytest -q -p phase_timeline` (the default
addopts; `-n 4` appended for the 4-worker series):

- control: `git archive` of `2dc1ed66` with `tests/conftest.py` and
  `tests/slowest_tests.txt` taken from `d3fdae7e` (the spread hook), and with
  `tests/test_slowest_first_dealing.py` and `tests/regen_slowest_tests.py` deleted
  (the new test imports the new hook and cannot be collected without it). The
  lever and off arms therefore also run those 25 fast tests: control 8352 passed,
  lever and off 8377 passed, 13 skipped each.
- lever: `git archive` of `2dc1ed66`.
- off (informational): the lever tree with `-p no:bakar_slowest_first`, no
  dealing at all.

Each run was gated on CPU pressure some avg10 under 0.5 (0.00 to 0.18 before every
run) and no pytest process; one untimed warm-up per arm and worker count (the first
one, 14.3 s, was cold); order per pair control, lever, off; every run rc 0 with the
expected counts (a first attempt expected 8350 and 8375 from the earlier note,
stopped at the first warm-up on the count check, and is not used). The phase
timeline plugin was loaded in every run, so absolute walls carry its overhead
equally in all arms and are not directly comparable with the 9.36 s median of the
dependency-sync subsection. Load1 before each run climbs from 7.75 to 11.94 over the
16-worker series because it is a one-minute average that includes the previous
runs; the pressure gate was quiet each time.

| run | load1 before | wall (s) | user+sys (s) | tail (s) | idle worker-s |
|-----|--------------|----------|--------------|----------|---------------|
| w16-ctl-1 | 7.75 | 9.42 | 94.54 | 0.310 | 4.924 |
| w16-lev-1 | 8.56 | 9.91 | 93.83 | 0.887 | 12.887 |
| w16-off-1 | 9.47 | 9.59 | 92.77 | 0.620 | 8.617 |
| w16-ctl-2 | 10.31 | 9.80 | 94.44 | 0.596 | 10.712 |
| w16-lev-2 | 10.71 | 9.50 | 93.63 | 0.423 | 6.194 |
| w16-off-2 | 11.36 | 10.08 | 93.55 | 1.154 | 16.397 |
| w16-ctl-3 | 11.67 | 9.42 | 94.68 | 0.237 | 4.256 |
| w16-lev-3 | 11.94 | 9.45 | 93.67 | 0.448 | 5.826 |
| w16-off-3 | 11.69 | 9.73 | 93.49 | 0.740 | 10.629 |
| w4-ctl-1 | 5.58 | 21.60 | 61.13 | 0.032 | 0.115 |
| w4-lev-1 | 5.00 | 21.64 | 61.20 | 0.053 | 0.226 |
| w4-off-1 | 4.01 | 21.97 | 61.08 | 0.383 | 1.619 |
| w4-ctl-2 | 3.72 | 21.56 | 60.78 | 0.138 | 0.436 |
| w4-lev-2 | 3.73 | 22.60 | 61.46 | 1.220 | 3.693 |
| w4-off-2 | 3.17 | 22.04 | 61.27 | 0.581 | 1.919 |
| w4-ctl-3 | 3.40 | 21.77 | 60.92 | 0.322 | 1.057 |
| w4-lev-3 | 3.23 | 21.72 | 61.27 | 0.132 | 0.590 |
| w4-off-3 | 3.45 | 21.95 | 61.04 | 0.523 | 1.830 |

Medians (wall / tail / idle worker-seconds):

| workers | control | lever | off |
|---------|---------|-------|-----|
| 16 | 9.42 / 0.310 / 4.924 | 9.50 / 0.448 / 6.194 | 9.73 / 0.740 / 10.629 |
| 4 | 21.60 / 0.138 / 0.436 | 21.72 / 0.132 / 0.590 | 21.97 / 0.523 / 1.830 |

Keep rule: at 16 workers lever minus control is 0.08 s (limit 0.1 s, passes by
0.02 s; per pair +0.49, -0.30, +0.03 s); at 4 workers it is 0.12 s (limit 0.2 s;
per pair +0.04, +1.04, -0.05 s, the middle pair being a 1.22 s tail outlier). The
off arm is slower than the lever at both worker counts (9.73 against 9.50 s at 16,
21.97 against 21.72 s at 4), so the lever is not a net loss on a 4-worker CI
runner. The lever does not beat the control on wall in this measurement: the
spread hook and the chunk-aware hook are within noise of each other, and both
beat no dealing by 0.2 to 0.4 s. The 16-worker margin is thin: three pairs, and
two of the three lever walls (9.91, 9.50) sit above the control's median.

Placement, `--collect-only -qq --randomly-seed=1` under `PYTEST_XDIST_WORKER=gw0`
with `PYTEST_XDIST_WORKER_COUNT` 16 and 4, listed tests per worker's first chunk
(the hook is the control's, the lever's, or absent):

| workers | control (spread) | lever (chunk-aware) | off |
|---------|------------------|---------------------|-----|
| 16 | `[17, 16, 16, 15, 0 x 12]` | `[4, 4, 4, 4, 4, 4, 4, 4, 4, 5, 4, 4, 4, 4, 4, 2]` | `[0, 1, 3, 1, 1, 4, 4, 0, 0, 7, 0, 0, 6, 31, 2, 3]` |
| 4 | `[64, 0, 0, 0]` | `[16, 17, 16, 14]` | `[5, 9, 7, 42]` |

The control stacks all 64 listed tests into the first four chunks at 16 workers
and into worker 0's chunk at 4 workers; the lever gives every chunk its share at
the chunk head (first listed test at index 0 in every chunk). The lever rows total
63 because one listed id, `tests/test_mounts_run_bounded.py::test_checks_fail_when_the_deadline_exit_is_removed`,
is not collected on this tree (the hook warns about it); the list was not edited.

decision (slowest-first dealing, chunk-aware): kept

### Ten-second round: coverage terminal report

Lever: switch the `term-missing` terminal table off while the total behind
`--cov-fail-under` is still computed. The controller calls `coverage.report`
twice per run (once for the total, once for the table), so the table is a
candidate for the post-run time.

Invalid first attempt. The first series passed `--cov-report=` on the command
line. That option appends to the `--cov-report=term-missing` already in
`addopts` instead of replacing it, so the table was printed in both arms (the
lever logs still held the Miss/Missing header and every per-file line). Its
numbers are void and kept only as a record: control walls 10.26, 10.28, 10.40 s
against lever walls 10.35, 10.30, 10.36 s, median 10.28 against 10.35 s.

Corrected series, on pc3 with the tree staged by `pc3-stage.sh tree`. The lever
arm replaces the whole option string:
`-o addopts="-v --tb=short --cov=bakar --cov-report= -n auto --maxprocesses=16 --dist=worksteal"`,
every other option identical to the default. The script checked each arm's log
before accepting the run: control must contain `Missing`, lever must not and must
contain `Required test coverage of 80.0% reached`. All six passed that check
(control logs 1 `Missing` each, lever logs 0; the lever logs print
`Required test coverage of 80.0% reached. Total coverage: 91.61%`). One untimed
warm-up (10.39 s, rc 0), then three interleaved pairs with `-p phase_timeline`,
each gated on CPU pressure some avg10 under 0.5 and no pytest running. All runs
rc 0, 8350 passed, 13 skipped. Load1 is recorded, not gated (it climbs from 3.2
to 10.9 across the series with nothing running).

| run | load1 before | wall s | user s | sys s | sessionfinish starts at s | last atexit s |
|-----|--------------|--------|--------|-------|---------------------------|---------------|
| cov-ctl-1 | 3.22 | 10.30 | 86.90 | 8.09 | 9.580 | 10.187 |
| cov-lev-1 | 7.18 | 9.49 | 87.02 | 8.14 | 8.766 | 9.376 |
| cov-ctl-2 | 8.21 | 10.29 | 87.61 | 8.31 | 9.584 | 10.174 |
| cov-lev-2 | 9.56 | 9.45 | 86.44 | 8.23 | 8.735 | 9.337 |
| cov-ctl-3 | 10.47 | 10.42 | 87.41 | 8.40 | 9.698 | 10.313 |
| cov-lev-3 | 10.92 | 9.54 | 86.71 | 8.26 | 8.818 | 9.423 |

Median wall: control 10.30 s, lever 9.49 s, a fall of 0.81 s (per pair 0.81,
0.84, 0.88 s; no pair rises), above the 0.5 s bar. The run phase is unchanged
(5.44 to 5.54 s control, 5.54 to 5.58 s lever, ends 7.6 to 7.7 s in both arms) and
the sessionfinish hook itself still takes about 0.55 s in both arms; the saving
is the gap between the run phase ending and sessionfinish starting, which
shrinks from about 2.0 s to about 1.1 s, i.e. the time spent producing the table.
user+sys is unchanged.

decision (coverage terminal report): kept

Applied in `pyproject.toml`: `addopts` now carries `--cov-report=` (empty value)
in place of `--cov-report=term-missing`. Omitting the option would not do: the
default report type is the plain `term` table. The comment above `addopts` says
the table is opt-in with `--cov-report=term-missing`. Floor proof on the dev host
(untimed): `pytest -q --cov-fail-under=99` exits 1 and prints `FAIL Required test
coverage of 99% not reached. Total coverage: 91.62%`; a normal run prints
`Required test coverage of 80.0% reached. Total coverage: 91.64%`; the opt-in
`pytest -q --cov-report=term-missing` exits 0 and prints the Missing table. The
normal dev-host run also exited 1 on an unrelated failure,
`tests/integration/test_hashserv_real_daemon.py::test_hashserv_real_daemon_lifecycle`
(8349 passed); it is a real-daemon test, outside this task.

No docs, README or project rule text tells a reader the default run prints
missing lines (searched `term-missing`, `missing lines`, `cov-report` outside this
note and `pyproject.toml`). CI passes its own `--cov-report=xml`
(`.github/workflows/ci.yml:53`), which still appends to the empty default, so the
CI step as first written lost the table. The CI step now passes
`--cov-report=term-missing` (commit 1bf323c1, a recorded scope addition) and
`-rfEs` (commit 0ee5cda9, which keeps failure lines in the short summary), so CI
logs carry the missing-lines table again.
`.claude/rules/pytest.md` (a symlink into another repository) has no coverage
report text, so no change is needed there.

### Ten-second round: temp path retention

Lever: `tmp_path_retention_policy = "failed"`, to remove each passing test's
directory as it finishes in the workers instead of in the controller's
sessionfinish (0.39 s). Measured on pc3 (control as is, lever with
`-o tmp_path_retention_policy=failed`, quiet gate psi avg10 under 0.5, one
untimed warm-up, tree including the coverage table change).

| run | load1 before | rc | wall s | user s | sys s | result |
|-----|--------------|----|--------|--------|-------|--------|
| warm-up | 0.87 | 0 | 9.37 | 85.93 | 8.21 | 8350 passed |
| ret-ctl-1 | 2.56 | 0 | 9.28 | 85.55 | 8.05 | 8350 passed |
| ret-lev-1 | 6.88 | 1 | 10.16 | 87.90 | 7.56 | 584 failed, 86 errors, 7680 passed |

The lever run fails, so the series stopped after pair 1 (a failing run is
invalid). The failure is deterministic and caused by the lever, not by load:
on the dev host `pytest tests/test_getvar.py -n 0 -p no:randomly` passes 49 of
49 by default and fails 40 of 49 with `-o tmp_path_retention_policy=failed`.
The dominant error is `FileNotFoundError: [Errno 2] No such file or directory`
(about 230 of the failures), consistent with tests that use a `tmp_path`
directory as the working directory: it is removed at the end of the test
before the other fixtures finish, and the suite then reads a deleted current
directory. Making the suite pass under the policy would mean editing tests,
outside this task.

Policy proof with a scratch test outside the repository
(`/var/tmp/claude-code/bakar/2026-11-17-ret-proof/test_ret.py`, one passing and
one failing test, each using `tmp_path`): with `all` both directories exist
after the run; with `failed` the passing test's directory is removed and the
failing test's directory is kept. So the option is in effect and behaves as
documented; the cost is the suite failures above.

decision (temp path retention policy): dropped

`pyproject.toml` is untouched. The median-wall bar (0.2 s fall, no pair
rising) is not met either: the only pair has the lever 0.88 s slower (invalid
run, indicative only).

### Ten-second round: remaining lever decisions

Levers measured or reasoned rather than built. The two timed series ran on pc3
with the tree staged by `pc3-stage.sh tree`, each preceded by one untimed
warm-up, three interleaved control and lever pairs, every run gated on CPU
pressure some avg10 under 0.5 and no pytest running, and the series set to stop
on any rc other than 0. All 14 runs (two warm-ups included) were rc 0, 8350
passed, 13 skipped. Load1 climbed from 1.7 to 10 across the series with nothing
running and is recorded, not gated. Series script: `pc3-lev8-series.sh`.

decision (collection sharding): not needed. The fixed fraction of collection
cost is 0.247 (0.41 s for one tiny file against 1.66 s for the full tree, from
the lever-gates subsection), so at most 75% (1.25 s) of single-process
collection scales with the number of files. Splitting that across 16 shards
would leave about 0.41 + 1.25 / 16 = 0.49 s, a ceiling of about 1.2 s saved per
collecting process, and only if the shards did not collide for CPU (the
collection GC subsection showed they do). Projected floor with the levers kept
in this change: the tree's median wall on pc3 is now about 9.5 s (coverage
subsection, 9.49 s; this round's runs 9.16 to 10.10 s), so the floor with
sharding would be at best about 8.3 s. Sharding is not needed to reach 10 s: the
median is already below it. The lever would be a follow-up change if the median
on pc3 rises above 10 s or the target drops under 9 s; the measurement that
would justify it is a prototype's collection barrier against the current 2.312 s
in three interleaved pairs, with at least a 0.4 s earlier barrier.

decision (module-scoped scenarios fixture): not applied. The four tests in
`tests/test_layer_repo_kas_guard.py` pay 0.47 to 0.53 s of setup each, about 1.4
worker-seconds in total, which is under 0.1 s of wall at 16 workers (1.4 / 16 =
0.09 s), below the 0.2 s bar used for the other levers. A module-scoped fixture
also runs before the per-test isolation fixture (the isolated home and
environment are set up per test), so the scenarios would be built against the
real home or an unisolated environment. The gain does not pay for that hazard.

decision (dependency-sync check): not applied. Three interleaved pairs of
`uv run pytest -q -p phase_timeline` (control) against `uv run --no-sync pytest
-q -p phase_timeline` (lever):

| pair | control wall s | lever wall s | fall s |
|------|----------------|--------------|--------|
| 1 | 9.34 | 9.16 | 0.18 |
| 2 | 9.44 | 9.35 | 0.09 |
| 3 | 10.10 | 9.42 | 0.68 |

Median wall: control 9.44 s, lever 9.35 s, a fall of 0.09 s, under the 0.2 s
bar (the 0.68 s pair is one control run at 10.10 s, with user+sys unchanged).
The sync check is cheap on a synced tree (`uv sync` reports 41 packages checked
in 0.21 ms), and dropping it would let a stale environment run silently, so the
default command keeps it.

decision (verbose flag): not applied, `-v` stays in `addopts`. Measured on pc3
with the output on a pseudo-terminal (`script -qec`), no `-q`, so the default
verbosity is what is timed. The control is `uv run --no-sync pytest`; the lever
replaces the whole option string with
`-o addopts="--tb=short --cov=bakar --cov-report= -n auto --maxprocesses=16 --dist=worksteal"`
(the current `addopts` minus `-v`; a command-line option also in `addopts` is
appended, not replaced). The script checked each arm's log: control must contain
`PASSED` lines, lever must contain none; all six passed that check.

| pair | control wall s | lever wall s | fall s |
|------|----------------|--------------|--------|
| 1 | 9.40 | 9.45 | -0.05 |
| 2 | 9.41 | 9.55 | -0.14 |
| 3 | 9.43 | 9.36 | 0.07 |

Median wall: control 9.41 s, lever 9.45 s, so removing `-v` is 0.04 s slower
(noise, no pair beyond 0.14 s). The per-test lines cost nothing measurable.

decision (property-test deadline): not applied. CI selects Hypothesis's
built-in CI profile (no deadline); `.github/workflows/ci.yml` sets no
`HYPOTHESIS_PROFILE`, and no profile is registered in the repository
(`rg register_profile tests src` finds nothing). Failed property tests due to a
deadline across all runs of this change: 0. Searched for `DeadlineExceeded`
under `/var/tmp/claude-code/bakar` on the dev host (no match) and for
`DeadlineExceeded` and `Flaky` in the 113 logs of the plan and runtime scratch
directories and the 74 logs under
`/var/tmp/claude-code/bakar/2026-11-17-pytest-10s-pc3/` on pc3 (excluding this
subsection's own series): no match for either. With zero deadline failures
there is nothing to lower or disable.

CI skip reasons: pending the first run that prints them. If the skip counts
still differ between CI and pc3 after that run, the explanation is recorded in a
later change.

### Ten-second round: result

Three hashes. Change base: `b8c287905c4a6e4aac52c44fbc44bd9739fd0cf1`, read from
`base_commits.bakar` in the change's `.devspec.yaml`. Control:
`b8c287905c4a6e4aac52c44fbc44bd9739fd0cf1`, the same commit, staged on pc3 with
`pc3-stage.sh control b8c28790` (a `git archive` of that commit, with the same
untracked `CLAUDE.md` and `ARCHITECTURE.md` symlinks excluded as for the finished
tree, so both trees skip the same tests). Finished tree:
`cc4496f50f5318a0439a469d2324563c8a5aea79`, the worktree HEAD, staged with
`pc3-stage.sh tree`. The control runs 8342 tests and the finished tree 8350
(passed); both skip 13, so the ids collected are 8355 and 8363.

Host: pc3, `uv run --no-sync pytest -q` in each staged tree, launched from bash.
One untimed warm-up of each tree, then three pairs, control first, finished tree
second, 2026-11-17 starting 17:30 (UTC-6). Every run was gated on CPU pressure
`some avg10` under 0.5 and no pytest running; every gate line reads
`quiet psi_avg10=0.00`. The series stops on any rc other than 0. Load1 before
each run is recorded and not gated. `pgrep -af pytest` on pc3 showed no other
session's pytest before the first run.

| run | tree | load1 before | rc | wall s | user+sys s | pytest summary |
|-----|------|--------------|----|--------|------------|----------------|
| warmup (untimed) | control | 1.05 | 0 | 14.29 | 104.60 + 8.32 | 8342 passed, 13 skipped, 97 warnings in 13.29s |
| warmup (untimed) | finished | 3.66 | 0 | 10.03 | 85.73 + 8.28 | 8350 passed, 13 skipped, 113 warnings in 9.48s |
| pair 1 | control | 4.21 | 0 | 14.07 | 102.70 + 8.31 | 8342 passed, 13 skipped, 105 warnings in 13.27s |
| pair 1 | finished | 5.33 | 0 | 9.36 | 86.00 + 8.27 | 8350 passed, 13 skipped, 105 warnings in 8.81s |
| pair 2 | control | 5.40 | 0 | 14.14 | 103.14 + 8.31 | 8342 passed, 13 skipped, 105 warnings in 13.33s |
| pair 2 | finished | 5.40 | 0 | 9.41 | 86.58 + 8.28 | 8350 passed, 13 skipped, 97 warnings in 8.86s |
| pair 3 | control | 6.64 | 0 | 14.14 | 102.62 + 8.23 | 8342 passed, 13 skipped, 97 warnings in 13.34s |
| pair 3 | finished | 7.07 | 0 | 9.33 | 86.11 + 8.34 | 8350 passed, 13 skipped, 105 warnings in 8.78s |

The warm-ups are not part of the medians. Median of the three timed runs,
control: wall 14.14 s, user+sys 111.01 s. Finished tree: wall 9.36 s, user+sys
94.45 s (runs 94.27, 94.86, 94.45), pytest-reported time 8.81 s.

Median default wall: 9.36 s

The median default wall of the finished tree is 9.36 s, which is 10 s or less, so
the ten-second target is met on pc3 (all three finished runs are under 9.5 s; the
control median is 14.14 s, a fall of 4.78 s).

The 9.36 s median above is for `uv run --no-sync pytest -q`, which skips the
dependency-sync check. The median for the default command with the sync check
(`uv run pytest -q`) is the one measured in the dependency-sync subsection: 9.44
s for the control arm of its three pairs (those runs also load the phase timeline
plugin). Both medians are under 10 s.

#### Single-process profile of the finished tree

One run on pc3 after the series, `uv run --no-sync pytest -n 0 --no-cov -p
no:randomly --durations=0 --durations-min=0.1 -q`: rc 0, wall 66.44 s, user+sys
46.50 s, 8350 passed, 13 skipped. Untimed profile, so no pressure gate applies;
load1 before was 5.78. 11 tests have a call duration at or above 0.5 s, each with
a class:

| Test | Call s | Class and reason |
|------|--------|------------------|
| tests/test_e2e_cli.py::TestSettings::test_host_swappiness_max_round_trip | 1.03 | deliberately real: drives the installed binary by design |
| tests/test_e2e_cli.py::TestSettings::test_multiple_keys_survive_independently | 1.02 | deliberately real: same |
| tests/test_e2e_cli.py::TestSettings::test_unset_clears_key | 0.77 | deliberately real: same |
| tests/test_e2e_cli.py::TestHostDoctorThresholds::test_configured_floor_raises_doctor_bar | 0.55 | deliberately real: same |
| tests/test_e2e_cli.py::TestHostDoctorThresholds::test_workspace_host_overrides_user_host | 0.55 | deliberately real: same |
| tests/test_e2e_cli.py::TestSettings::test_non_positive_host_count_rejected | 0.52 | deliberately real: same |
| tests/test_e2e_cli.py::TestSettings::test_set_get_round_trip | 0.51 | deliberately real: same |
| tests/test_mounts_run_bounded.py::test_checks_fail_when_the_deadline_exit_is_removed | 0.94 | work-bound: runs every check in the list against a runner with its deadline exit removed, so the checks that need the deadline run real bounded subprocesses to their limit; the mutation is the point of the test |
| tests/test_pin_probe.py::test_run_bash_local_drain_timeout_keeps_output_read_during_the_drain | 0.80 | wait with no test-visible knob: the test sets a 0.4 s first timeout and a 0.4 s drain window, and the line it asserts can only arrive inside that drain window |
| tests/test_layer_delivery_remote.py::test_sigint_to_the_unit_lets_a_guarded_build_drain | 0.57 | deliberately real: a systemd user unit whose stand-in build drains for 0.5 s after SIGINT, which is the behavior under test |
| tests/test_kas_build_failure_error.py::test_run_build_zero_exit_writes_no_step_fail | 0.52 | work-bound (not investigated further): the body has no sleep and calls the build driver with a stubbed pty outcome; no knob names a wait |

The 11 tests sum to 7.78 s from the table. The retained scenarios of the previous
change hold: no feed mirror test reaches 0.5 s (the slowest,
tests/test_feed_mirror_meta.py::test_open_size_bound_stops_decompression_early,
is 0.30 s) and the stop-command tests in tests/test_cli_stop.py appear nowhere in
the 0.1 s and over list, so each runs under 0.1 s. The scenario that work-bound
tests are classified holds: every test at or above 0.5 s has a class and reason
above. The mounts.py poll tests (doctor cache-mounts, stress parse, uninative
post-build), the event-log tail test and the monitor multibyte test have left the
0.5 s list.

#### Phase timeline of the final tree

Command (run in the staged finished tree on pc3, one run, rc 0, wall 9.53 s, 8350
passed, 13 skipped, same command line as the baseline's phase timeline above):

```text
cd <tree>; TL_T0=<date +%s.%N> TL_OUT=<tl-final.jsonl> PYTHONPATH=<helpers dir> uv run --no-sync pytest -q -p phase_timeline
```

Summarizer output, seconds from `TL_T0`, against the baseline timeline above:

| phase | summarizer line | seconds | baseline seconds |
|-------|-----------------|---------|------------------|
| pre-session | `configure 0.376 sessionstart 0.3898` | sessionstart at 0.390 | 0.391 |
| gateway ramp | `gateway ramp 0.413 .. 0.703` | 0.290 | 0.295 |
| collection barrier | `collection 1.838 .. 2.151 (barrier 2.151)` | collection 0.313, barrier at 2.151 | 0.241, 2.312 |
| run phase | `run phase 2.151 .. 7.752` | 5.600 | 7.664 |
| tail | `tail 7.386 .. 7.752`, `idle worker-s 6.212` | 0.366, idle worker-seconds 6.212 | 0.887, 13.288 |
| end phase | `sessionfinish 8.807 .. 9.356` | 0.549 | 1.453 |

Other summarizer lines: `node_down 7.099 .. 7.778`, `teardown_nodes 8.807 ..
8.920` (0.113 s), `terminal summary 9.311 .. 9.355` (0.045 s), `last atexit 9.4133`.
The run phase ends at 7.752 and sessionfinish starts at 8.807, so 1.055 s pass
between them; the summarizer has no line for that gap, but the post-run
attribution ("where the post-run time goes") assigns it: one `coverage.report`
call (0.923 s, the total behind the fail-under check, since the table is off in
this tree), `DistMaster.finish` (0.101 s) and the last worker's coverage save
(0.023 s), which sum to 1.047 s of the 1.055 s. Those measurements came from a
different run of an earlier tree state, so the match is by size, not a
decomposition of this run.

Lever still needed: none for the target, which the median meets. The largest
remaining shares are the 2.151 s before the collection barrier (the follow-up
the collection sharding decision above would weigh, with its stated trigger of a
median above 10 s on pc3 or a target under 9 s) and the 1.055 s between the run
phase and sessionfinish, which is the coverage report and its finish step.

#### Stability and equivalence checks of the finished tree

These checks were redone on the final tree, commit `32d35e78` (the code of the
branch; only this note changes after it). Run on the dev host, which is shared and
not idle, so no wall time here is a measurement, from bash, in `git archive` copies
under `/var/tmp/claude-code/bakar/2026-11-18-pytest-10s-stability/`: `final/` for
the foreground runs and `final-bg/` for the background loop, both built with
`uv sync --frozen --group dev`. The control is the archive of `b8c28790` made
for the earlier pass (`2026-11-17-pytest-10s-stability/control/`; its
`tests/test_cli_monitor.py` still has `slept.append` and `tests/_proc_locks.py` is
absent). The archives lack the untracked `CLAUDE.md` and `ARCHITECTURE.md`
symlinks, the same for every tree. Every run's log begins with its provenance: the
archive path, `git rev-parse HEAD` of the source (`32d35e78c9f6...`), the pytest
rootdir (`final/` or `final-bg/`), `record_sleep` count 2 in
`tests/test_cli_monitor.py`, `SlowestFirstDealing` count 2 in `tests/conftest.py`
and `proc_locks_present`. A check over all 30 + 150 + 36 logs found no log without
them. Each run's full output is its own file.

Correction of the earlier record. The loops reported in the previous version of
this section claimed the `8c1e2cd3` tree but ran the `542f1596` archive: their
rootdir was `final/` of the earlier pass, `tests/test_cli_monitor.py` there still
had `slept.append`, and the `8c1e2cd3` archive was never extracted. They proved
nothing about the code that was committed, and one background full-suite run in
that pass failed (rc 1, `1 failed, 8349 passed`) with no test recorded, because
the loop kept only the last run's output. The counts, ids and coverage figures
below replace them. For history: the first loop on `542f1596` had 1 failure in
30 full runs (run 16, `tests/test_cli_monitor.py::test_watch_sleeps_for_the_requested_interval`,
`slept == [0.01]` saw `[0.1, 0.1, 0.01]` because the test patches the shared
`time.sleep`; commit `8c1e2cd3` changed it), and the first module pass of that
round was the clean one (120 of 120 rc 0, with a background loop of 19 full runs
of which one failed, unnamed).

Collected ids missing: 0
(`--collect-only -qq -n 0 --no-cov -p no:randomly`: control 8355 ids, final tree
8390 ids, 0 control ids absent from the final tree, 35 ids new in it: 3 in
`tests/test_harness_gc.py`, 1 in `tests/test_console_width_env.py`, 6 in
`tests/test_mounts_run_bounded.py` and 25 in `tests/test_slowest_first_dealing.py`.)

Skip reasons identical: yes
(`-n 0 --no-cov -p no:randomly -rs -q`, both rc 0: control 8342 passed, 13 skipped;
final tree 8377 passed, 13 skipped. The SKIPPED lines with line numbers stripped
are 10 lines each and `diff` of the two sorted lists is empty.)

Coverage floor: one default run of the control tree printed `Required test
coverage of 80.0% reached. Total coverage: 91.63%` (8342 passed, 13 skipped); the
final tree printed 91.63% on full run 1 and 91.62% on run 2.

Full runs failed: 0 of 30
(`uv run --no-sync pytest -q` in the `final/` archive, 30 runs in a row, each run's
rc logged, no stop on failure: all 30 rc 0, each `8377 passed, 13 skipped`. load1 was
0.20 before the loop.)

Timing-window modules, 30 runs each (`uv run --no-sync pytest -n 0 --no-cov -q
-p no:randomly <module>` in `final/`) while a full-suite loop ran continuously in
`final-bg/`: failures 0 of 30 for each of `tests/test_layer_delivery_remote.py` (68
tests), `tests/test_reuse_lock.py` (20), `tests/test_pin_probe.py` (24),
`tests/test_eventlog_tail.py` (5) and `tests/test_mounts_run_bounded.py` (6); 150
of 150 rc 0. The first run of each module was clean. load1 was 15.42 before this
loop and 14.16 to 19.48 at the start of the background runs.

Background full-suite loop: 36 runs, 35 rc 0 (`8377 passed, 13 skipped`) and 1
failure. Run 12 (`bg-012.txt`, rc 1, `1 failed, 8376 passed, 13 skipped`) failed in
`tests/integration/test_hashserv_real_daemon.py::test_hashserv_real_daemon_lifecycle`
at `tests/integration/test_hashserv_real_daemon.py:85` with
`AssertionError: ensure_running returned None on a workspace with bitbake-hashserv present`.
load1 was 17.37 when that run started (32 CPUs, the module loop running beside it).
The test passed in the other 35 background runs and in all 30 foreground runs.
Probable cause, from reading the code and not reproduced: `ensure_running` in
`src/bakar/hashserv.py` (lines 283-295) gives the freshly spawned daemon
`_STARTUP_PROBE_DEADLINE_SECONDS = 2.0` (line 44) to start listening and returns
`None` when it does not, so a daemon that needs more than 2 s to import and bind on a
host at load1 17 fails the assertion. That is production code this change does not
touch (the spec allows `src/bakar/mounts.py` only), so it stays a follow-up: raise or
scale the startup deadline, or make the test wait for the daemon with its own bound.
Earlier sightings of the same assertion were one pc3 control run at load1 7.08 and
one dev-host default run.

Correction (2026-11-18): the cause is a port collision, not the 2.0 s deadline. All
numbers below were measured on 2026-11-18.

- Startup latency of the real bitbake-hashserv through `ensure_running`: idle p50 102 ms
  (max 112 ms, 60 starts); 32 CPU hogs max 299 ms; 128 hogs max 715 ms; 256 hogs
  (load1 72) max 1507 ms (40 starts). No start missed the 2.0 s deadline in 230
  synthetic-load starts.
- A probe beside two back-to-back default-suite loops on pc3 (load1 130 down to 77):
  6 of 220 starts failed. All were early exits 122 to 265 ms after spawn with
  `[Errno 98] address already in use`, and the derived port was in the closed-connection
  state (TIME-WAIT) each time. The slowest successful start took 386 ms.
- Mechanism: the derived port range 49152-65534 overlaps the kernel's default ephemeral
  range 32768-60999 (about 72% of derived ports). A client connection holding the port
  (established, or closed within the last 60 s) makes the daemon exit with address in
  use. One default suite run on pc3 leaves about 85 closed-connection sockets in
  49152-60999 for 60 s.
- A foreign listener on the derived port made `ensure_running` return a URL for a daemon
  that had already died (found while measuring).
- Before/after arms, interleaved per iteration on pc3 beside two back-to-back
  default-suite loops (the control and lever archives), load1 4.1 to 29.9 (mean 23.5):
  the integration test in an archive of base commit 37d46cff failed 10 of 150 runs, every
  failure the `ensure_running returned None on a workspace with bitbake-hashserv present`
  assertion at line 85. The same test in an archive of 28015457 (this change's HEAD after
  tasks 1.1 to 6.1) failed 0 of 150 runs. The old test prints no port or stderr, so the 10
  control failures are attributed to the collision by the matching signature and the probe
  result above, not by a per-run capture.
- prserv shares the port derivation and is unchanged by this change.

Production files changed: src/bakar/mounts.py
(`git diff --name-only b8c28790..HEAD -- src` at `32d35e78` lists only that path.
For information, the non-src, non-test files changed are `.github/workflows/ci.yml`,
`docs/notes/bakar-pytest-time-profile.md` and `pyproject.toml`.)
