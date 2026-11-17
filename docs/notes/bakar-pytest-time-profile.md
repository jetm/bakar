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

## Ten-second round

### Baseline default runs

Host: pc3 (rog-strix-x870i-jetm), AMD Ryzen 9 9950X3D 16-Core, 32 threads,
governor performance, Python 3.14.10, uv 0.13.8. The runs were taken on pc3,
not on the dev host, with `uv run --no-sync pytest -q` in a staged tree. The
series ran 2026-11-17 starting 16:37 (UTC-6). load1 was 4 to 6 at the starts
while CPU pressure (`some avg10`) read 0.00, so the gate waited on pressure
under 0.5 and no running pytest, not on load1. Every gate line reads
`quiet psi_avg10=0.00 after 0s`.

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
passed, 13 skipped):

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
s, 8342 passed, 13 skipped):

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
  `bakar` entry without it). It collected 8355 tests; 1312 import lines. The
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
