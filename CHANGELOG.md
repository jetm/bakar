# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Added `bakar build --on <host>` for remote single-host build dispatch. It preflights the ssh host, rsyncs the working tree (uncommitted edits included) to the identical path with a `--delete` mirror gated behind a dry-run preview and confirmation (`--yes` bypasses the prompt) and a cache/artifact exclude set, then execs `bakar build` remotely fish-safely over `ssh <host> bash -s` with sccache-dist off by default (`--sccache-dist` opts back in). The remote output streams live, the remote run-id and a copy-pasteable `ssh <host> bakar triage <run-id>` line are surfaced on completion, and the remote build's exit code propagates.

## [0.22.0] - 2026-07-15

### Added
- Buildtools-extended toolchain installs are now scoped to the openembedded-core release commit, preventing a toolchain built for one Yocto release (e.g. scarthgap) from silently satisfying a build against a different release. The `BAKAR_BUILDTOOLS_DIR` environment variable remains an unconditional override.
- `bakar prefetch` now accepts an `--image` / `-i` flag to specify an arbitrary fetch target without modifying configuration files. Workspaces backed by a raw bbsetup environment are now supported, defaulting to `core-image-minimal` when no image is given for generic or bbsetup BSP families.
- `bakar monitor` now shows ccache hit/miss statistics for plain ccache builds (previously only sccache-dist cluster information was shown, even when ccache was the active cache). The display mirrors the build-time UI: sccache-dist active shows cluster and daemon stats, otherwise ccache active shows hit/miss rate.
- The normalized bitbake-events artifact (schema v3) now captures PSI (Pressure Stall Information) samples and disk pressure events, including `MonitorDiskEvent`, `DiskUsageSample`, and `DiskFull` records. Schema version bumped to 4 with the addition of a `cache_backend` field on task rows.
- PSI pressure samples collected during a build are now persisted to `psi-samples.json` in the run directory after the build completes, enabling post-hoc analysis of CPU, IO, and memory pressure trends.
- Disk usage is sampled every 5 seconds during a build and persisted to `disk-samples.json` in the run directory, providing visibility into storage consumption over the build lifetime.
- Added `bakar insights` command to analyze a completed build run's performance from its persisted artifacts. Reports four independently-selectable sections: sstate cache hit/miss breakdown per recipe, per-task wall-clock timing ranked by duration with historical baseline annotations, PSI CPU/IO/memory pressure time-share with a dominant-resource verdict, and disk-usage growth with an optional threshold warning. Defaults to the latest run under the workspace's search roots when no explicit run ID is given.
- `bakar stop` now correctly tracks and signals the detached `bitbake-server` process (which leads its own session via `bb.daemonize`'s double-fork). Previously, `bakar stop` reported "stopped" while the real bitbake-server continued dispatching tasks.
- `bakar stop` now accepts a `--timeout` option to bound the graceful-wait escalation period. The corresponding `[build] stop_grace_seconds` config key (default `0`, preserving the previous unbounded wait) allows a default grace period to be set for non-interactive callers such as scripts.
- The sccache distribution overlay now correctly handles recipes that select the clang native toolchain via `TOOLCHAIN_NATIVE` (e.g. `libcxx-native`, `compiler-rt-native`) or via `TCOVERRIDE` (e.g. `chromium-ozone-wayland`), rather than silently overwriting their compiler selection back to gcc. `clang-native` is also added to the sccache distribution allow-list.
- Every build now includes a cache-classify overlay that attaches a per-task cache backend annotation (sccache, ccache, or none) to the normalized event log and the live build UI. The cache backend is visible as a badge column in the running-task table and as a `cache=` token in the plain-text status line.
- The live build UI and `bakar monitor` now display a per-task cache backend badge indicating which compile cache backend (sccache, ccache, or none) is handling each running task.
- Added a `show_baseline_drift` boolean configuration option (`[build] show_baseline_drift`, env `BAKAR_SHOW_BASELINE_DRIFT`, default `false`) that gates loading of historical task-duration baselines. When disabled, the live UI skips baseline I/O entirely.

### Changed
- The buildtools-extended download size estimate shown during `bakar setup` is corrected from ~63 MB to ~500 MB, and the description now notes that `install-buildtools` fetches silently via `wget -q`, explaining why the step produces no output for several minutes.
- The GLIBC locale generation cap (`en_US.UTF-8` only) has been removed from the generic tuning overlay. Full locale generation now runs by default, satisfying locale `RDEPENDS` (e.g. from `m4-ptest`) without requiring per-recipe bbappends, at the cost of a slower cold `glibc-locale` build.

### Fixed
- `bakar prefetch` no longer passes the literal string `"generic"` to bitbake as a fetch target for generic/BYO kas YAML workspaces. It now correctly defaults to `core-image-minimal`.
- `bakar prefetch`'s bbsetup path now respects image overrides from environment variables, workspace config, and presets; previously these were silently discarded. A `ValueError` from an unresolvable machine now produces a clean error message instead of a raw traceback.
- `bakar insights --timing` now correctly annotates tasks with historical baseline data using the BSP/machine/mode-scoped timings file that real builds write to, rather than a default path that is never populated.
- `bakar insights` no longer crashes with a `MarkupError` when a `DiskFull` event message contains bracketed paths (e.g. `[/mnt/data]`) in Rich markup output.
- The critical-path computation in `bakar insights --timing` now correctly weights graph nodes by recipe duration rather than edges, and strips version suffixes from recipe names before looking up durations, so the longest blocking chain is computed correctly on real build data.
- `bakar insights --pressure` no longer misreports a dimension with no usable readings as a measured `0.0%`; it now correctly signals data unavailability in that case.
- `bakar insights --disk` no longer reports `0 bytes` of growth when the disk sampler fired exactly once during a short build; it now correctly signals unavailability when fewer than two samples are available.
- The cache-classify layer is now materialized on disk before every build and `bakar bitbake` invocation. Previously the overlay referenced a layer path that was never created, which would have caused a kas parse failure on every real build.

## [0.21.0] - 2026-07-08

### Added
- Added `--full` mode to `clean-cache` that performs a complete cold-reset of the sccache-dist cluster: stops hashserv/prserv daemons, empties the shared sstate directory in-place (preserving NFS export inodes), wipes build directories, clears the local sccache client cache, wipes the local ccache directory, and resets the sccache-dist server on all nodes over SSH. Includes `--dry-run` support, a live-build guard (refuses to run while bitbake is active unless `--force` is given), and post-reset verification that distributed compilation recovered.
- Added a `cluster` boolean setting (default `false`) to user and build configuration, controllable via `BAKAR_CLUSTER`, that gates cluster-mode preflight checks so a single-node build is never blocked by cluster-only diagnostics.
- Added cluster-mode doctor preflight checks for the central hashserv and prserv endpoints (`check_central_hashserv`, `check_central_prserv`): an unreachable endpoint is a blocking failure, an unset or loopback endpoint is a warning. These checks only run when cluster mode is enabled.
- Added a cluster-mode doctor preflight check (`check_shared_cache_mounts`) that verifies the shared sstate, downloads, and ccache directories are on NFS mounts and are writable. A non-NFS or unwritable sstate/downloads directory is a blocking failure; a local ccache directory is a warning.
- Added `--plain` / `--ci` and `--rich` global flags to force plain or Rich output mode. Plain mode produces ANSI-free, glyph-free, line-oriented output suitable for CI logs; Rich mode forces the interactive display even when output is piped. Passing both flags exits with an error.
- Build output now automatically switches to plain (ANSI-free) mode when stdout is not a TTY or a CI environment variable is set, so piped and CI logs are readable without manual flags.
- `bakar monitor` now produces plain, ANSI-free output in non-TTY and CI environments (or when `--plain` is passed), including the `--once` path. `--json` output is unaffected.
- The build UI now shows a live cache badge displaying the cumulative compiler cache hit rate and, for sccache-dist builds, a distributed-compilation verdict alongside existing build progress.
- A per-build cache summary line is now printed after the build completes, showing the delta hit/miss counts for the build rather than lifetime daemon totals. The line is prefixed with `bakar[cache]` for reliable CI log parsing.
- Added ccache statistics persistence to the build run directory (`ccache-stats.json`), providing a structured artifact for post-build cache analysis comparable to the existing sccache stats artifact.
- `bakar report` now reads and displays ccache hit/miss counters from `ccache-stats.json` when present, shown as a distinct section in both human-readable and JSON output. The label correctly reflects whether the counters represent this build or lifetime totals.
- For BYO builds (`bakar build some.yml`), the post-build artifacts path and hints now correctly reflect the `machine:` value declared in the kas YAML rather than the generic placeholder.
- `bakar layers --status` now issues a single `bitbake -e` call to retrieve all layer status variables instead of one subprocess per variable, significantly reducing startup overhead.
- `bakar inspect --recursive` now reads the `pn-buildlist` file instead of parsing `bitbake -g` stdout, eliminating build-log noise lines (progress messages) from the dependency list.
- Elapsed build time is now printed on the build-succeeded line, so the total duration is visible after the live UI has closed.
- Daemon stderr from central hashserv/prserv and sccache server processes is now redirected to a persistent log file under the user state directory instead of being discarded, making silent startup failures diagnosable.
- The normalized bitbake events artifact now includes the `run_id` of the build that produced it, making it unambiguously linkable to its originating run.
- `bakar doctor` now continues running all remaining checks even when an individual check crashes, recording the failure as a structured result rather than aborting the entire diagnostic run.

### Changed
- The glibc binary locale generation in the generic tuning overlay is now limited to `en_US.UTF-8` by default, reducing cold build time for glibc-locale. Workspaces or distros that require additional locales can override this setting.
- The `bakar doctor` host-tools check no longer prefixes its message with "GENERIC" for BYO/generic-family builds, where the vendor prefix carries no meaningful information.
- The eventlog artifact schema version is bumped to v2; the `preset`, `release`, and `per_recipe` fields are removed from the normalized output. Downstream consumers such as build-insights and triage can detect the format change via the `schema_version` field.
- `bakar show` now exits with code 2 for unrecognized `--format` values, consistent with `changelog` and `drift`.
- `bakar stop`, `for-all`, `prefetch`, `dump`, `lock`, and `log` now correctly accept `-f <yaml>` to specify a kas YAML, matching the behavior of other subcommands.
- `bakar init` now creates the workspace directory before writing the marker file, preventing a `FileNotFoundError` for bbsetup and generic families.
- `bsp-detect` no longer classifies all `am*` machines as TI; only Sitara-generation boards (`am3xx`/`am4xx`/`am5xx`/`am6xx`) are matched.
- `hashserv stop()` now verifies process identity before sending any signal, preventing accidental signaling of an unrelated process that has recycled the recorded PID. A `PermissionError` during signaling is handled gracefully rather than propagating.
- A corrupted or invalid vendor configuration file now produces a descriptive `ValueError` instead of a raw library exception; BSP family detection falls back to built-in patterns rather than failing.
- Passing a `bsp_family` that conflicts with an active preset's declared family now raises an error immediately rather than silently using the caller-supplied value.
- A docker/podman container runtime mismatch in `bakar cluster-info` is fixed: the active runtime is now detected rather than hardcoded to `docker`, so podman hosts are handled correctly.
- `changelog` and `drift` now validate the `--format` argument before performing any git work, failing fast on an invalid value.
- The build-dir removal progress display no longer shows a time-remaining column, which previously showed unreliable estimates due to uneven subtree sizes.
- Passing an invalid or missing `machine` in a bbsetup config now raises an error with a clear message at translation time rather than emitting `machine: null` into the kas YAML and failing later with an opaque schema error.
- `kas shell` failures (non-zero exit code) are now correctly reported as `step_fail` in the event log instead of `step_ok`.
- Shell command failures in `run_shell` and `run_shell_capture` are now reported as `step_fail` with the exit code, instead of always reporting `step_ok`.
- `stress-parse` now correctly distinguishes a run where no fork-race signature matched but the exit code was nonzero (recorded as `errored`) from a genuinely race-free pass, preventing inflation of the fix-confidence rate.
- `bakar settings set build.cluster false` now correctly stores and reads back a boolean `false` instead of the truthy string `"false"`, which previously silently enabled cluster mode.
- The `hashserv` daemon now reuses its known port on restart instead of respawning on a fixed port after unlinking a stale port file, preventing collisions with a live daemon.
- `lock` now runs `repo manifest` under the configured `bsp_root` instead of a hardcoded path, fixing manifest locking for non-default vendor repo layouts.
- `bakar doctor` crash isolation now preserves the crashing check's registered severity, so a crash in a blocking check remains blocking rather than being silently downgraded to a warning.
- Git subprocess calls in `layers`, `manifest_diff`, and `pin_state` now share a centralized helper with a five-second timeout, preventing indefinite hangs on wedged or NFS-stalled repositories.
- Artifact persistence errors (event log copy, event JSON write, task timing write) after a completed build are now demoted to console warnings and never cause the build to exit with a failure code.
- A corrupted entry in the build timings baseline no longer aborts the build; the malformed entry is silently skipped, consistent with how `load_baselines` handles the same condition.

### Fixed
- Fixed `clean-cache --full --dry-run` no longer triggers a live `sccache --dist-status` subprocess call (which could auto-start the client daemon) when only printing the plan.
- Fixed `clean-cache --full` now exits with code 2 when no sstate directory can be resolved, instead of silently wiping a guessed path.
- Fixed `clean-cache --full` now stops hashserv/prserv daemons before emptying sstate, preventing live daemons from running against unlinked files.
- Fixed `clean-cache --full` prserv stop now correctly resolves the binary from the workspace root and respects the configured `cluster_bind_host`.
- Fixed a file descriptor leak in central daemon and sccache server spawn paths where a failure inside `Popen()` could leave the stderr log file open.
- Fixed duplicate terminal step events being emitted when an exception occurred during post-build artifact persistence.
- Fixed the interrupted-step detector incorrectly flagging normally-completed steps as interrupted; it now recognizes all three terminal event types (`step_ok`, `step_fail`, `step_skip`).
- Fixed I/O errors during event emission (full disk, closed file descriptor) no longer propagate out of the error-reporting path, preserving the "never raises" contract.
- Fixed the `triage` command disagreement between JSON and text mode on a parsed-but-clean artifact with a kas-level failure.
- Fixed the hashserv port and PID readers now handle corrupt state files (`ValueError`) and permission errors in addition to missing files, preventing unexpected exceptions on the not-running path.
- Fixed `lock` running `repo manifest` under a hardcoded path instead of the configured workspace root.
- Fixed a typo in the fork-race hint that read "not a recipe Manual workaround" instead of "not a recipe bug. Manual workaround".
- Fixed IPv6 addresses (both bare `::1` and bracketed `[::1]`) being incorrectly parsed in `split_host_port`, causing healthy IPv6 cluster endpoints to be reported as unreachable by doctor.
- Fixed `bakar report` ccache window label always showing "this build" regardless of whether the persisted artifact contained a lifetime or per-build window marker.
- Fixed the `run_shell_capture` step always reporting success regardless of exit code; non-zero exits now produce `step_fail` events with structured exit code information.

### Removed
- Removed `scripts/clean-all-cache.sh`; its functionality is now available as `bakar clean-cache --full`.
- Removed the `preset`, `release`, and `per_recipe` fields from the normalized eventlog artifact (schema v2).

## [0.20.0] - 2026-07-03

### Added

- `bakar setup` can now provision a central Rust/PostgreSQL-backed hashserv+prserv tier for build clusters that share an `SSTATE_DIR`. Unlike bitbake's single-writer SQLite daemons, the central services are concurrent-writer-safe and keep the PR database off the volatile `TMPDIR`, so hash-equivalence and PRs stay consistent and monotonic when multiple nodes report to one shared sstate. Their endpoints are persisted as the `build.bb_hashserve` and `build.prserv_host` config keys; a build that finds them set points `BB_HASHSERVE`/`PRSERV_HOST` at the shared tier and skips the per-workspace daemons entirely.
- Added a `bakar prserv start/stop/status` subcommand (mirroring `bakar hashserv`) that manages a bakar-owned PR service keyed to the shared sstate, bound to a cluster-reachable address, so PRs persist with the cache lineage across builds and `TMPDIR` wipes and are reachable from other cluster nodes.
- Added a `cluster_bind_host` config key that makes the managed `hashserv`/`prserv` daemons bind and advertise a reachable address instead of `localhost`, so a single shared daemon can serve every node of a build cluster. The default is unchanged (`localhost`), keeping single-node builds untouched.
- Added `bakar sched-triage`, a read-only command that aggregates the sccache-dist scheduler journal and client log into a single report: scheduler misroute rate (bucketed by load), cluster saturation against the admission ceiling, per-compile timers, preprocess-concurrency gauge, and local fallbacks plus remote rustc errors. `--json` emits the structured report and `--events <bitbake-events.json>` joins the poll series to live `do_compile` supply for per-phase cluster utilisation.
- `bakar setup` now auto-provisions the pinned `buildtools-extended` toolchain required by host-mode builds: it runs the workspace's own `install-buildtools` script into a `$HOME` directory and persists the location as the `build.buildtools_dir` config key so `detect_buildtools` resolves it in a fresh shell without a manual `BAKAR_BUILDTOOLS_DIR` export. A new host-mode `bakar doctor` preflight check blocks the build when the toolchain is missing or its native gcc cannot execute against the host kernel ABI. See [docs/setup.md](docs/setup.md).
- `bakar report` now surfaces build-performance metrics: a per-language sccache hit/miss/hit-rate map, a per-node distribution count map, a per-task-family wall-time rollup (`do_compile`/`do_configure`/`do_install`/`do_fetch`/other) and a Go compile subtotal, sourced from build-end stats persisted to the run directory. Both the JSON payload and the human output carry the data, and the human output omits each section when its underlying data is absent.
- `bakar cluster-info` now shows the per-language cache hit/miss breakdown and labels per-language cache (`cache[<lang>]`) separately from the aggregate per-node distribution (`dist[<node>]`), so a per-language miss count is no longer misread as that language having distributed to a node.
- The end-of-build sccache summary now breaks cache hits and misses down per language (C/C++, Rust, Assembler), each with its own hit-rate percentage, instead of reporting only aggregate totals.
- The `bakar build` live UI now surfaces cluster and cache status during the build: sccache-dist builds show a cluster-load line plus the in-daemon cache/distribution line, and ccache builds show a ccache hit/miss line, so distribution health is visible without running `bakar monitor` in a second window.
- `bakar monitor` now shows runqueue progress (`<done>/<total> tasks (<n> left) <pct>%`) and a real elapsed clock derived from the run directory, counts setscene re-runs separately from real task failures (a noisy-but-healthy build no longer reads as collapsing), and reports the managed `hashserv`/`prserv` addresses — or the central-tier endpoints when configured — each with a liveness probe.

### Changed

- The `-w`/`--workspace` flag now changes directory into the resolved workspace before any path resolution, across every command that exposes it, so a relative kas YAML positional argument resolves from outside the workspace (e.g. `bakar stop -w <ws> machine.yml`). An invalid `-w` path (missing or a regular file) exits 2 naming `--workspace`. `bakar init`'s `--workspace` is unchanged.
- `bakar stop` replaces its fixed 60-second grace poll with an unbounded, task-aware graceful wait: it waits until the build process/container is no longer running, rendering live `Waiting for N running tasks to finish` progress from the build event log, with a spinner + elapsed fallback when task progress is unavailable or the log freezes. If the container runtime becomes unreachable across repeated liveness queries it gives up and exits 1; Ctrl-C during the wait escalates to SIGTERM then SIGKILL, and `--force` skips the graceful wait entirely.
- Host execution is now the structural default and the `kas-container` path is an explicit opt-in. A configured container image no longer selects the container on its own; reach the container path with the new global `--container` flag, the `BAKAR_CONTAINER` env var, or a `[build] container` toggle in the workspace/user config. The old `--host` flag, `BAKAR_HOST_MODE`, and `[build] host_mode` are retained as no-op back-compat aliases so existing configs keep parsing unchanged.
- Host-mode builds now enforce the pinned `buildtools-extended` toolchain and refuse to start (`BuildtoolsMissingError`, raised before bitbake is invoked) when it cannot be located via a sourced sysroot, `BAKAR_BUILDTOOLS_DIR`, or the persisted `build.buildtools_dir`, instead of silently falling back to the rolling system gcc and producing non-reproducible builds.
- sccache-dist now distributes compiles on an allow-list (`SCCACHE_INCLUDED_PN`) rather than a deny-list. Only a curated set of heavy-object recipes (the toolchain/LLVM C++ set and large C++ feed packages) route through the shared sccache daemon and the cluster, where the per-object compile cost dwarfs the network round-trip; every other recipe now compiles locally. The non-distributed tail is cached with ccache (the two launchers are now complementary rather than mutually exclusive, so ccache and sccache-dist can be enabled together), giving heavy recipes distribution and the long tail local object caching.
- `PARALLEL_MAKE` and `BB_NUMBER_THREADS` are now derived automatically whenever unset: `PARALLEL_MAKE` tracks the live sccache-dist scheduler CPU total (auto-scaling as servers join) or the local core count otherwise, and `BB_NUMBER_THREADS` is bounded by host RAM and capped at 4× local cores, with a relaxed per-recipe RAM divisor under an actually-reachable sccache-dist cluster to keep more `do_compile` phases in flight. An explicit config value still wins per field.
- Rust compiles (`rust-native` and cargo recipes) are now cached and distributed through sccache via a `RUSTC_WRAPPER` shim that routes rustc through the cluster without tripping cc-rs into wrapping the C compiler; previously they ran plain `rustc` on one node with no caching and no distribution.

### Fixed

- Host-mode sccache-dist builds now actually distribute recipe compiles across the cluster. Previously autotools `do_configure` baked `CC=gcc` into the generated Makefile so `do_compile` never invoked sccache, and `do_compile`'s private network namespace hid the pre-started daemon; sccache now sets `CCACHE` globally and pre-starts a unix-domain-socket daemon that crosses the netns boundary, so every task reaches the shared daemon and native/cross recipes compile on the cluster.
- `bakar stop` now scans run directories newest-first for the first still-targetable run instead of inspecting only the lexically latest one, so a live build is stopped even when a later clean-recipe or second build left a newer but dead run directory.
- `bakar stop` now removes the stale `bitbake.lock` and `bitbake.sock` files from the build TOPDIR after the build process is confirmed no longer running, so the next build is not blocked by a leftover lock; the `bitbake-cookerdaemon.log` is preserved for post-mortem analysis.
- Host-mode `bakar monitor`, `bakar report`, and the build-end stats now read the host client's sccache daemon over its unix socket, so the common (containerless) build path surfaces per-language and per-node distribution data instead of reporting the daemon absent.
- `bakar getvar`, `bakar dump`, and `bakar shell` now print a clean `Error:` line naming the missing host toolchain (and the fix) instead of a raw traceback when `buildtools-extended` is absent.
- Fixed BSP (NXP/TI) family detection being lost when `-w`/`--workspace` chdir'd to the workspace root from inside a BSP subdirectory; the invoking directory is now captured before the chdir so `bsp_from_cwd` still resolves `ti`/`nxp`.
- `BAKAR_SCCACHE_DIST=0` now correctly disables the sccache-dist overlay even when `config.toml` has it enabled, restoring the documented `BAKAR_*` env-override contract and a clean no-sccache baseline.
- `bakar setup`'s git-identity remediation now writes the identity where the check reads it (via `git -C <probe_dir> config`, not `git config --global`), so it no longer aborts the setup plan when launched outside a git repo and no longer targets a location the check never inspects.
- Clean/from-scratch builds no longer abort at parse in OE's connectivity sanity check; bakar's generic tuning overlay now sets `CONNECTIVITY_CHECK_URIS = ""` (surviving `bakar clean`), since all sources come from the shared `DL_DIR` and the reachability probe is redundant here.

## [0.19.0] - 2026-06-26

### Added

- Added `--sccache-dist` and `--sccache-scheduler` global options (placed before the subcommand, e.g. `bakar --sccache-dist build my.yml`) enabling distributed compilation via sccache-dist across all commands that resolve a build config, including `build`, `getvar`, `dump`, `shell`, `clean-recipe`, and `bitbake`
passthrough.
- Added `sccache_dist` and `sccache_scheduler_url` keys to the `` section of `config.toml` for persistent distributed-compile configuration.
- Added a `bakar-tuning-sccache` kas overlay and a companion `meta-bakar-sccache` BitBake layer (`sccache.bbclass`) that replace ccache with sccache as OE's compiler launcher, grant network access to compiler-bearing tasks, handle cmake recipes, ptest tasks, and kernel-specific tasks, and emit a per-node distribution summary at build end.
- Added a preflight check (`doctor`) for sccache-dist that verifies the `sccache` binary is on PATH, the scheduler is TCP-reachable, the client is not in Disabled state, the `` token is configured, and an end-to-end compile actually reaches the cluster; reports scheduler capacity (server count, CPU count, in-progress jobs) in the preflight message.
- Added `bakar cluster-info` command to query sccache-dist scheduler capacity (servers, CPUs, in-progress jobs, per-node breakdown) and, when a build container is running, report the in-container daemon's cache hit/miss, distributed vs. local compile counts, and a DISTRIBUTING / LOCAL-ONLY verdict.
- Added `bakar monitor` command providing a single refreshing view of a running distributed build: cluster load, bitbake task progress and failures, and build liveness. `--json` emits a single snapshot; `--json --watch` streams NDJSON for CI consumption.
- Added `--sccache-dist` and `--sccache-scheduler` options to `bakar getvar` and `bakar dump` so a distributed build's resolved variables and flattened YAML can be inspected without modifying `config.toml`.
- Added a `bakar-tuning-ccache` overlay that is selected only when ccache is the effective launcher; ccache and sccache-dist are now mutually exclusive by overlay selection rather than add-then-remove.
- Added `ccache` (default `true`) and `rm_work` (default `false`) as `` keys in `config.toml` and `.bakar.toml`, resolved with env (`BAKAR_CCACHE` / `BAKAR_RM_WORK`) > workspace > user-config > default precedence.
- Added `nproc`, `parallel_make`, and `bb_number_threads` as independent `` keys in `config.toml`, allowing compile `-j` and recipe concurrency to be tuned separately (useful for sccache-dist clusters where compile parallelism should span the whole cluster).
- Added `kas_container_image` to the workspace `.bakar.toml` `` table, allowing a per-workspace container image pin that overrides the global `config.toml` setting.
- Added `bakar clean my.yml` (BYO kas-YAML positional) to clean the `workspace/build-<stem>` directory for meta-avocado and other generic BSP builds.
- Added a `scarthgap` (Yocto 5.0 LTS) qemuarm64 example kas config (`examples/kas-qemuarm64-scarthgap.yml`) alongside the existing wrynose example.
- Added a qemuarm64 example kas config (`examples/kas-qemuarm64-wrynose.yml`) for cross-compile validation.

### Changed

- `bakar --host` (and `--sccache-dist` / `--sccache-scheduler`) are now global options placed before the subcommand; placing them after the subcommand is rejected by the parser.
- `bakar clean-cache` now falls back to mtime on `relatime` mounts (Linux default) in addition to `noatime`; only `strictatime` retains the atime-based eviction policy.
- `bakar clean-cache` large prune operations now run in parallel with a progress bar and ETA instead of deleting files serially with no output.
- `bakar clean --all` no longer stops the hashserv daemon when it is shared across workspaces via a common `SSTATE_DIR`; only a workspace-local daemon is stopped.
- The `container_image` config key has been renamed to `kas_container_image` in `config.toml`, `.bakar.toml`, and all related fields; existing config files are migrated automatically on first load.
- Setting `build.kas_container_image` in `config.toml` now correctly propagates `KAS_CONTAINER_IMAGE` to the container subprocess (previously it was honored by `doctor` but silently ignored by the actual build).
- The hash-equivalence database (`BB_HASHSERVE_DB_DIR`) is now co-located with `SSTATE_DIR` so the equivalence index is shared across workspaces that share an sstate cache.
- Workspaces that share an `SSTATE_DIR` now share a single hashserv daemon and DB keyed to that directory, so equivalence mappings computed by one build are visible to sibling builds.
- The hashserv preflight check now reports PASS (at INFO) when the daemon is not yet running but `bitbake-hashserv` is available (the build will start it), and only warns when the binary itself is absent.
- The `bakar doctor` preflight report is now grouped into labelled sections (Compute & parallelism, Tools & container runtime, Caches & storage, Host tuning, Workspace & build config) for easier reading.
- The container-OS Python-version check has been removed; builds using Python 3.14 containers (including `jetm/kas-build-env:5.3-f44`) are no longer blocked.
- The git-identity preflight check now probes a workspace sub-repo (where `includeIf "gitdir:..."` conditionals fire) rather than the workspace root,
eliminating false BLOCK results for developers whose identity comes from per-project `includeIf` rules.
- Memory floor calculation now excludes zram-backed swap (which is physically backed by RAM) and includes only disk-backed swap, preventing false PASS results on zram-heavy machines; the diagnostic message surfaces the excluded zram amount.
- Tuning overlay keys are renamed to a `zz-bakar-NN-*` sort-last scheme so bakar's `local.conf` assignments reliably win over workspace overlay sections.
- `build-dir` removal (`bakar clean`) is now parallelized using the same thread-pool primitive as `clean-cache`, significantly reducing wall time on large OE `tmp/` trees.
- The `bakar doctor` sccache-dist preflight check is now scoped to host-mode builds; in container mode it warns when the configured scheduler URL points to localhost (unreachable from inside the container) instead of probing host-side.

### Fixed

- `bakar getvar` and `bakar dump` now apply tuning overlays (sccache, hashequiv, shared-cache) consistently with `bakar build`, so `getvar CC --recipe <target>` correctly shows the sccache launcher prefix when `--sccache-dist` is active.
- Container-mode sccache-dist builds now actually distribute compiles; previously, `kas`'s `clean_environment` scrubbed `SCCACHE_CONF`, `SCCACHE_DIR`, and `SCCACHE_DIST_SCHEDULER_URL` before bitbake ran, causing the in-container daemon to start config-less and compile everything locally.
- Container-mode sccache-dist builds now fail fast at `BuildStarted` when the scheduler is unreachable or the client is not distributing, instead of silently compiling everything locally for the entire build.
- `bakar build --sccache-dist` with a broken auth token no longer silently falls back to local-only compilation; the preflight guard parses `SCCACHE_CONF` for a `` token and runs an end-to-end compile probe that exercises the token-gated allocation path.
- `bakar --sccache-dist build` now bakes the resolved `-j N` parallelism value directly into the materialized tuning overlay, preventing `kas`'s environment scrubbing from silently reverting the compile parallelism to the default of 16.
- Meta-avocado builds with `--sccache-dist` no longer fail at parse with "layer directories do not exist"; the `meta-bakar-sccache` layer is now materialized under the workspace directory that `KAS_WORK_DIR` points to.
- The `bakar doctor` preflight sccache check no longer reports a false BLOCK for container builds where the host-side TCP probe cannot speak for the
in-container client path. - `bakar doctor` no longer falsely blocks host-mode meta-avocado builds that require `gfortran` or `git-lfs`; those tools are now checked in the preflight and
reported with actionable fix hints before the build starts.
- Workspace `.bakar.toml` sections written as scalars instead of tables (e.g. `build = "x"`) now raise a clear error naming the offending section instead of being silently ignored.

### Changed

- `bakar clean-cache` now falls back to mtime (creation date) for sstate eviction on `relatime` mounts, not just `noatime`. `relatime` updates atime at most once per 24h and any full-tree read (a backup, `du`, or a file indexer) resets every file's atime at once, so atime is not a dependable last-read signal there. Only `strictatime` mounts still use atime. This fixes "Nothing to remove" on relatime filesystems whose atimes were clobbered by a cache-wide scan. See [docs/clean-cache.md](docs/clean-cache.md).
- `bakar clean-cache` sstate deletion now runs across a thread pool and shows a progress bar with an ETA. The previous serial unlink of a large prune (hundreds of thousands of files, hundreds of GiB) ran silently and looked hung; `os.rename`/`os.unlink` release the GIL, so the pool parallelizes the actual disk work.

### Removed

- Removed the `container-os` doctor check that BLOCKed builds on container Python 3.13/3.14. The parser failures it guarded against (the 3.13 fork-in-multi-thread deadlock and the 3.14 forkserver `_pickle.PicklingError`) are handled by the `PYTHONMALLOC=malloc` mitigation in the bakar tuning overlays and by bitbake 5.3+ forcing the `fork` multiprocessing context, so the version gate was a false positive against working images such as `jetm/kas-build-env:5.3-f44`.

## [0.18.0] - 2026-06-18

### Added

- Added `bakar stop`, a command that gracefully halts a running `bakar build` without corrupting in-flight recipe workdirs. It dispatches on execution mode: container builds are resolved by a `bakar.run_id` label injected at launch and signalled inside the container so bitbake runs its own graceful shutdown, while host builds receive a process-group SIGINT, then a 60-second grace period, then SIGTERM and SIGKILL. `--force` skips the SIGINT grace wait; a positional kas YAML targets BYO/generic workspaces (mirroring `bakar build`/`bakar log`), and `--manifest`/`--workspace` resolve NXP/TI workspaces. The persistent `bitbake-hashserv` daemon and other workspaces are never touched. See [docs/stop.md](docs/stop.md).
- Added unclean-stop detection: when a prior build was killed without `bakar stop` (a raw `kill -9`, power loss, or OOM kill), the next `bakar build` detects the stale launch record at startup and prints an advisory warning naming the interrupted step and the run's `kas.log`. The warning never blocks or auto-repairs the build.
- Added a `check_override_syntax` pre-flight check to `bakar doctor` that flags deprecated Yocto override syntax (`VAR_append`, `VAR_prepend`, `VAR_remove`, including function-definition and `${PN}` forms) in active layers. Recipe files (`.bb`, `.bbappend`, `.inc`) produce a BLOCK finding and `.conf` files a WARN. The check gates on the local bitbake version (the underscore form was dropped in bitbake 2.8 / scarthgap) and skips gracefully when bitbake is absent or older.
- Added cross-validation checks to `bakar layers inspect` that surface `LAYERSERIES_COMPAT` mismatches against the distro codename, duplicate `BBFILE_PRIORITY` values, and orphaned `.bbappend` files whose base recipe is not provided by any active layer.
- Added a `--json` flag to `bakar doctor` and `bakar triage` for machine-readable output.
- Added support for colon-separated kas overlay syntax (`machine.yml:overlay.yml`) in `bakar getvar`, matching the overlay handling already in `bakar build`, `bakar bitbake`, and `bakar dump`.
- `bakar build` now prints a log hint after the build UI exits, showing the exact `bakar log` command (with the run ID) to follow that run's full build log.

### Changed

- Doctor pre-flight checks now always run. The `--skip-doctor` flag and the `[build] doctor` config key (both of which skipped the checks entirely) are replaced by the global `--hide-doctor-report` flag and the `[build] show_doctor_report` config key (default `true`). These hide the report but never skip the checks: only build-blocking issues print when hidden, and a BLOCK-severity finding still aborts the build. Existing configs are migrated automatically - `[build] doctor = false` becomes `[build] show_doctor_report = false` (config schema version 1 → 2). See [docs/build.md](docs/build.md), [docs/config-reference.md](docs/config-reference.md).
- `bakar layers inspect --json` now returns an object with `layers` and `cross_validation_warnings` keys instead of a bare list of layer records; consumers of the JSON output must read the `layers` key.

## [0.17.0] - 2026-06-16

### Added

- Added `bakar setup`, a once-per-machine host preparation command that profiles the host, maps failing `bakar doctor` host-environment checks to remediation actions, and applies them: unprivileged actions (kas install via `uv tool install`, docker image pull, git identity, cache directory creation) run inline; privileged actions (sysctl drop-in, docker `daemon.json` merge, `systemctl enable --now docker`, `usermod -aG docker`) are assembled into a single auditable `set -euo pipefail` script piped to one confirmed `sudo bash -s` via stdin — never written to disk. `--dry-run` prints the host profile and the full generated script without touching anything. `--yes` skips the confirm gate and requires passwordless sudo, exiting non-zero with a clear message if unavailable rather than hanging on a prompt. See [docs/setup.md](docs/setup.md).
- Added a `[host]` configuration section (in both `~/.config/bakar/config.toml` and workspace `.bakar.toml`) that controls the thresholds `bakar doctor` host-environment checks compare against: `host.inotify_instances`, `host.inotify_watches`, `host.swappiness_max`, `host.nofile_soft`, and `host.mem_min_gb`. Defaults equal the values doctor previously hardcoded, so verdicts are unchanged until a value is written. Precedence is workspace `.bakar.toml` `[host]` > user `config.toml` `[host]` > built-in floor. All five keys are accessible via `bakar settings set/get/unset`. See [docs/settings.md](docs/settings.md), [docs/config-reference.md](docs/config-reference.md).
- `bakar setup` writes the applied sysctl and docker ulimit values to the global `[host]` config so a follow-up `bakar doctor` verifies the machine against what `setup` applied.
- `bakar setup` remediates the sysctl check by writing a removable `/etc/sysctl.d/99-bakar.conf` drop-in (never `/etc/sysctl.conf`) covering `fs.inotify.max_user_instances`, `fs.inotify.max_user_watches`, `fs.inotify.max_queued_events`, `vm.swappiness`, and `fs.file-max`, then reloading with `sysctl --system`.
- `bakar setup` merges `default-ulimits.nofile` and `storage-driver: overlay2` into `/etc/docker/daemon.json` via a `python3` JSON round-trip that preserves all pre-existing keys and backs up the original to `daemon.json.bakar.bak` before the first write.

### Changed

- `bakar doctor` host-environment checks (`check_sysctl`, `check_docker_ulimits`, `check_memory`) now read thresholds from the resolved `[host]` configuration rather than hardcoded literals; failure messages include the configured threshold value. Behavior is identical until a `[host]` value is written.

## [0.16.0] - 2026-06-12

### Added

- Added `bakar rebuild <recipe>` command that chains `bitbake -c cleansstate <recipe> && bitbake <recipe>` in a single kas-container invocation with full run logging and exit-code fidelity; `--keep-going`/`-k` applies to the build half only.
- Added `--target`/`-t` option to `bakar build` that passes `--target <TARGET>` to kas (e.g. `bakar build machine.yml --target avocado-complete`); unset preserves existing behavior of building the YAML's own target.
- Added support for kas colon-overlay syntax (`machine.yml:overlay.yml`) in `bakar bitbake`, `bakar build`, and `bakar dump`; extra overlays are validated at invocation time and merged after the bakar tuning overlay so user settings win over bakar defaults.
- Added a stall watchdog (`build.stall_abort_secs`, default `2700`) that SIGINTs the build when every running task's log has been silent past the threshold, then records a `stall-timeout` step_fail naming the wedged task instead of spinning indefinitely. Set to `0` to disable. Configurable via `bakar settings set build.stall_abort_secs <N>`.
- Added `SDKMACHINE` passthrough to the kas-container environment so SDK-target builds (`SDKMACHINE=x86_64 bakar build ... --target avocado-complete`) pick the correct SDK architecture.
- Added overlay stack listing to the build-start log line, naming every overlay in the merge chain (including user-supplied colon overlays) so operators can confirm their extra overlay was applied.

### Changed

- Container pre-flight probes (`check_container_os`, `check_container_bitbake`) now retry once on a cold-start timeout so a single slow docker daemon startup no longer silently disarms the BLOCK gate for broken Python 3.13/3.14 containers.
- `bakar report` no longer reports `peak build/tmp` size; the background `du -sb` sampler that collected it has been removed. The `du.tsv` artifact is no longer written to run directories.
- The live build UI now correctly resets its progress bar and phase between chained bitbake invocations (e.g. `bakar rebuild`'s `cleansstate && build`), so the second run displays its own parse → setscene → tasks cycle rather than showing a stale "full" bar during parse.
- Installation instructions updated: `bakar` can now be installed from PyPI with `uv tool install bakar` or `pip install bakar`.

### Removed

- Removed `peak_tmp_bytes` field from `bakar report` output and `--json` output; the `du.tsv` artifact is no longer produced in run directories.
- Removed the `time.log` artifact (`/usr/bin/time -v` wrapper) from run directories; nothing in bakar read it.

## [0.15.1] - 2026-06-05

### Added

- Added `BB_DEFAULT_EVENTLOG` passthrough to all BSP tuning overlays, ensuring the live build UI's event tailer reliably follows the per-run event log; previously the variable was scrubbed by bitbake's `clean_environment` and every event-driven UI feature (sstate ratio, parse cache note, failure alerts, log preview) silently fell back to the knotty regex parser.
- Added support for include-only kas YAML configs (files with `header.includes` but no `machine:` or `repos:`) to BSP family detection; wrapper YAMLs now inherit the base file's NXP or TI family overlay, and any non-empty include list that does not resolve to a known family is accepted as `generic` rather than rejected with an error.
- Added a failure-injection example config (`examples/kas-qemux86-64-wrynose-fail.yml`) for exercising the live UI's failure-surfacing features; it forces `m4-native` to miss sstate and fail `do_configure` deterministically, including with `--keep-going`.

### Changed

- The live build UI now commits the pipeline header, sstate ratio, and failure count into the scrollback **above** each task failure's output: when bitbake's first error line for a task is detected on the PTY feed, the Live region is stopped before that line prints, streams the failure block as plain output, then restarts — so the log reads top-to-bottom without the status frame landing below the error text.
- Failure alert blocks now print below the frozen frame as a self-contained group (✗ FAILED line, host log path, last 15 log lines) rather than as a line above the live region; the count is deduplicated so a failure detected via the PTY head line and later confirmed by a `TaskFailed` event does not increment the counter twice.
- The sstate reuse line in the live build UI was moved from between the build bar and the task table to directly after the pipeline breadcrumb, so the display reads top-to-bottom: pipeline state → cache reuse → progress bar → task table.
- The error message shown when a kas YAML cannot be classified now mentions `header.includes` as a valid alternative to `machine:` and `repos:`.

## [0.15.0] - 2026-06-05

### Added

- Added a live streaming event reader that follows the growing build event log during a build, so the display receives authoritative bitbake events in real time rather than waiting for the build to finish.
- Added per-task duration baselines stored in `~/.local/state/bakar/task-timings/` (scoped per workspace, machine, and build mode) that accumulate Welford online statistics across builds, giving the stuck-task detector a historical reference independent of the current run's median.
- Added a parse-setscene-tasks breadcrumb header that shows which lifecycle phase the build is currently in, with completed phases marked by a check, the active phase highlighted, and future phases dimmed.
- Added a log tail preview under each task failure: when a task fails, the last 15 lines of its build log are rendered dimmed below the failure summary.
- Added a parse cache efficiency note to the parse-complete announcement, reporting the percentage of recipes loaded from cache and how many were re-parsed (e.g. "92% cached, 38 re-parsed") or explicit all-from-cache / cache-empty messages at the extremes.
- Added real-time failure alerts that print above the live display during `--keep-going` builds as soon as each task failure is detected, including the recipe name, task name, and translated log path.

### Changed

- The live build display is now driven by decoded bitbake events (task counts, setscene reuse, parse progress) instead of regex-scraping the knotty terminal text, providing authoritative progress numbers across bitbake releases. The regex feed remains active as a fallback when no event log is available.
- Layer hashes are now displayed as a table at the start of the build, as soon as `bblayers.conf` materialises, rather than after the build finishes. All build paths (manifest, BYO, bbsetup) and all layer-related commands (`sync`, `report`, `layers`) use the same table format.
- The layer table now resolves a containing remote branch name for layers pinned at detached-HEAD commits, so rows show a meaningful branch rather than a blank field.
- The setscene reuse line now renders as a percentage with a will-build count ("92% sstate (412 cached, 38 will build)") instead of a raw ratio, making remaining work visible at a glance.
- Stuck-task highlighting now uses per-recipe historical baseline means (yellow past 2×, red past 4×) when available, and shows a drift timer on tasks that exceed 4× their reference. Without a baseline file the previous median-based path is unchanged.
- Task-timing baselines are now keyed by `<recipe>:<task>` (version/revision suffix stripped) so different recipes never share a baseline, and a version bump retains its history. Files from the previous schema are discarded and re-accumulated in one build.
- PSI pressure thresholds in `config.toml` are stored as percentages (0–100) and converted to bitbake's microseconds-per-second unit (0–1,000,000) at the environment boundary, correcting a mismatch where a calibrated value of 20 (percent) was passed to bitbake as 20 µs/s instead of 200,000 µs/s.
- PSI autocalibration is now ratchet-up-only: thresholds are raised when an unthrottled build observes higher peaks, but never lowered. A light or heavily cached build no longer drops thresholds that were trained on a heavier workload. To recalibrate from scratch, delete the `pressure_max_*` keys.
- The `nproc` doctor check now reports the derived `BB_NUMBER_THREADS`, `PARALLEL_MAKE`, and `BB_NUMBER_PARSE_THREADS` values alongside the raw `NPROC`, and notes any user overrides found in `local.conf`.
- The global build timer is now rendered inline directly after the build pipeline segment (bold foreground) rather than right-aligned at the far terminal edge in a dim style.
- Task table columns now grow to the widest cell seen during the run and never shrink, preventing columns from jumping left and right as recipes start and finish.
- The "tasks" pipeline segment now appears only when real (non-setscene) tasks actually execute; fully sstate-cached builds end at "✓ parse ── ✓ setscene" without an unused queued segment.
- The setup (parse) progress bar's elapsed clock is now backdated to the bakar start time, matching the build bar, so the parse-phase duration is no longer under-reported.
- The layer list is now rendered as a headed table (Layer / Commit / Branch / Version columns) matching the style of the doctor pre-flight diagnosis table.

### Removed

- Removed the `--psi-calibrate` flag from the `doctor` command. The `[build] psi_autocalibrate` setting covers the same calibration automatically during every build and writes the converged values back to `config.toml`, making the interactive flag redundant.

## [0.14.0] - 2026-06-04

### Added

- Bakar now captures and persists a normalized bitbake event log (`bitbake-events.json`) in each run directory, recording which tasks ran, which failed, and the associated log file paths. This artifact is produced for both full builds and recipe-level (`bakar bitbake`, `bakar clean-recipe`) invocations, regardless of build outcome.
- The `bakar triage` command now reads structured failure information directly from `bitbake-events.json`, showing the failing recipe, task, and a translated log file excerpt without relying on regex scraping of `kas.log`. Falls back to `kas.log` analysis when the structured artifact is absent.
- `bakar triage` gained `--run`, `--preset`, and `--release` selectors for builds that produce multiple run directories (e.g. preset fan-out). When no selector is given, triage defaults to the most-recent run directory that contains a failure.
- The run directory now contains two new files documented in the configuration reference: `bitbake_eventlog.json` (raw bitbake event log) and `bitbake-events.json` (normalized schema with `schema_version`, `build`, `tasks`, `setscene`, and `failures` fields).

### Fixed

- Bitbake's `BB_DEFAULT_EVENTLOG` environment variable was previously dropped by `kas-container`'s allowlist before reaching Docker, so the event log was never written to the run directory. The variable is now injected via `--runtime-args -e` so it survives the container boundary.
- When `BB_DEFAULT_EVENTLOG` injection failed, the event log written to OE-core's default location (`build/tmp/log/eventlog/`) was not discovered. Bakar now searches that directory for the newest file whose modification time is at or after the build-start timestamp and copies it into the run directory before normalizing.
- `bakar dump` and `bakar lock` no longer raise an error when deriving the container-side event log path, which previously failed because their temporary run directories lie outside the bind-mount tree.
- Preset fan-out triage now correctly discovers run directories for NXP/TI targets, which are one subdirectory level deeper than `bbsetup`/`generic` targets and were previously missed by the glob pattern.

## [0.13.0] - 2026-06-04

### Added
- Added named preset support: define reusable BSP build configurations (for nxp, ti, bbsetup, and generic families) in `~/.config/bakar/config.toml` under `[[presets]]` tables, or ship presets via `vendors.toml`.
- Added `bakar presets list` command that displays all configured presets in a table showing name and family.
- Added `bakar presets show <name>` command that prints full preset details including family, machine/distro/image settings, and per-release build targets.
- Added `bakar presets add` interactive wizard that guides through family-appropriate fields and writes a new `[[presets]]` entry to `config.toml`.
- Added `bakar presets remove <name>` command that removes a user-defined preset from `config.toml` (vendor presets cannot be removed).
- Added `--preset <name>` flag to `bakar build` that selects a named preset, with tab-completion support for preset names.
- Added multi-release preset fan-out: when a preset defines multiple releases, `bakar build --preset <name>` runs all releases sequentially and prints a summary table (Release, Status, Duration), exiting 1 if any release fails.
- Named preset fields (machine, distro, image, manifest, kas_yaml) sit between workspace `.bakar.toml` overrides and user `config.toml` defaults in the precedence stack; explicit CLI flags always win.
- Each preset build writes into a dedicated subdirectory under `build/` named after the preset metadata (e.g. `<distro>-<machine>-<version>` for nxp/ti, `<image>-<machine>` for bbsetup/generic), preventing collisions between releases.
- Added fish shell completion support via `scripts/gen-fish-completion.py`, which generates a native fish completion file including dynamic `--preset` name completion (Typer's built-in completion does not support fish).
- Added documentation for the named preset system (`docs/presets.md`) and shell completion setup (`docs/completion.md`).

### Fixed
- A malformed `[[presets]]` block in `config.toml` now causes any `bakar` subcommand to exit with a clear error message at startup (exit code 2) rather than failing silently or crashing later.
- Unknown preset names passed to `--preset` now exit with a clear error message (exit code 1) instead of producing a confusing traceback.

## [0.12.0] - 2026-06-04

### Added

- Added `bakar drift` command, which compares each workspace source's pinned revision against its checked-out HEAD and reports per-source pinned SHA, actual SHA, and commit distance for all drifted repositories. Supports `--all` to include clean sources, `--json` for machine-readable output, and `--format md` for markdown output. Exits 0 when no drift is detected.
- Added `bakar changelog <from> <to>` command, which generates release notes summarising what changed between two workspace states (manifest XML files, kas lockfiles, or git refs). Produces Added, Removed, and Modified sections; Modified layers include a commit count and git log excerpt. Supports `--format md` for headed markdown output.
- Added `--dry-run-script PATH` option to `bakar build` and `bakar sync`. Writes a self-contained, executable bash script reproducing the full build or sync invocation to the given path, or to stdout when `-` is passed. The existing `--dry-run` preview behaviour is unchanged.
- Added `check_sstate_hash_leak` doctor check. Scans `build/conf/local.conf` and sibling `.conf`/`.inc` includes for host-specific variables (`DATETIME`, `BUILD_REPRODUCIBLE_BINARIES`, `PWD`, `USER`, `HOME`, `HOSTNAME`) assigned without a `[vardepsexclude]` annotation, which corrupt sstate task signatures and cause unnecessary cache misses. Reports a warning with the exact remediation. Skipped before workspace sync when `local.conf` does not yet exist.
- Added forward migration for `config.toml`. Older config files without a `config_version` field are automatically migrated to the current schema and saved. A `config_version` higher than the current version raises an error naming the unsupported version.
- Added `generic` and `bbsetup` as valid vendor families in `vendors.toml`. Previously only `nxp` and `ti` were accepted, preventing declaration of bbsetup or generic vendor boards without patching the source.
- Added a full configuration reference page (`docs/config-reference.md`) documenting every option across `config.toml`, `.bakar.toml`, and `vendors.toml`, all `BAKAR_*` environment variables, types, defaults, and annotated examples.
- Added a coverage badge to the README reflecting live Codecov results.

### Changed

- Loading `.bakar.toml` now emits a warning for any unrecognised key, naming both the unknown key and the valid keys for that section. Config loading still succeeds; the warning is never a hard failure.
- Fixed two errors in `docs/configuration.md`: the `vendors.toml` example used `[[vendor]]` instead of the correct `[[vendors]]`, and `manifest_pattern` instead of the correct `manifest_regex`.

## [0.11.0] - 2026-06-03

### Added

- Added `bakar bitbake <recipe> [-c <task>] [-k]` command to build individual recipes with task targeting, run logging, and live knotty progress display (the same progress bar as `bakar build`). The `devshell` task opens an interactive terminal session and `listtasks` pretty-prints the available task names.
- Added `bakar clean-recipe <recipe>` alias for running `bitbake -c cleansstate` on a single recipe.
- Added `bakar graph <recipe>` command to analyze a recipe's BitBake dependency graph. Reports package count, direct dependencies, transitive dependency (blast radius), longest build chain, cycle detection, and most depended-on recipes. Supports `--format {text,dot,json}`, `--depth N`, and an optional buildhistory runtime fan-in section when buildhistory data is present.
- Added `bakar mirror <git-url>` command to seed an offline `PREMIRROR` from a git URL. Clones bare-and-mirrored, then produces a `git2_<netloc><path>.tar.gz` tarball with `--owner oe:0 --group oe:0` and an mtime fixed to the committer date, making the output byte-stable across re-runs of the same revision. Output goes to `--output-dir`, the configured `DL_DIR` when inside a workspace, or the current directory.

### Fixed

- Fixed `bakar graph` always reporting zero packages, no longest chain, and no critical recipes. Container startup log lines emitted by kas were prepended to the captured `task-depends.dot` content, causing the DOT parser to fail silently and return an empty graph; the log preamble is now stripped before parsing.
- Fixed `bakar graph` exiting with success and printing empty graph data when artifact retrieval (`cat ${TOPDIR}/task-depends.dot`) failed due to a wrong TOPDIR, missing artifact, or permission error. The command now checks retrieval exit codes and rejects an empty or malformed TOPDIR, exiting non-zero with the captured error message.
- Fixed `bakar graph --format dot` running the full analysis pipeline (including buildhistory reads and pn-buildlist retrieval) before discarding all results to echo only the raw dot text; the dot path now returns immediately after the dot content is captured.

## [0.10.0] - 2026-06-03

### Added

- Added `bakar show` command that prints the fully-resolved build picture (Config, Overlays, Layers, Sources, and the exact `kas-container` command that would run) without invoking a container. Works on un-built workspaces; supports `--json` and `--format md` for Markdown output.
- Added `bakar getvar` command that resolves BitBake variable values inside `kas-container`. Supports global lookup, recipe-scoped lookup (`--recipe`/`-r`), unexpanded values (`--unexpanded`/`-u`), and assignment history showing the full include-chain of file:line assignments (`--history`).
- Added `bakar inspect` command that produces a per-recipe report combining Identity, Paths, Sources, Inherits, Packages, and Dependencies sections from two container calls (`bitbake-layers show-recipes` and `bitbake -e`).
- Added `bakar diffsigs` command that diagnoses why a recipe task missed sstate cache by running `bitbake -S printdiff` followed by `bitbake-diffsigs`, then rendering structured output: root cause first, an indented rebuild chain with depth count and cross-recipe boundary annotation, and a compact added/removed dependency diff instead of full lists. Use `--raw` to bypass parsed output and receive the full unprocessed capture.
- Added `bakar layers inspect` sub-command that reports per-layer priority, series compatibility, version, and provides information by combining local `layer.conf` parsing with container-backed `bitbake-layers show-layers`. Accepts `--json`.
- Added `bakar layers status` sub-command that fetches the effective `MACHINE`, `DISTRO`, `DISTRO_CODENAME`, thread counts, mirror URLs, and hashserv URL for the current project in a single container call. Accepts `--json`.

### Changed

- `bakar layers` is now a sub-app; running it bare continues to print the existing git short-hash and branch listing unchanged.
- All container-dispatching commands (`show`, `getvar`, `inspect`, `diffsigs`, and existing commands such as `doctor`, `shell`, `diff`) now accept a kas YAML path via the `-f`/`--manifest` flag in addition to a positional argument, routing it correctly through the kas dispatch path instead of treating it as an XML manifest.
- `bakar inspect` now extracts `WORKDIR`, `S`, `B`, `D`, and `T` paths from the `bitbake -e` environment dump instead of a separate `bitbake-getvar` call, reducing the number of container invocations from three to two per run.
- `bakar diffsigs` structured output now includes chain depth ("N levels deep"), a cross-recipe boundary note when the root cause originates in a different recipe, a cause count when multiple dependency changes are found, and a count summary on the dependency diff ("N added, N removed").

## [0.9.0] - 2026-06-03

### Added

- `bakar report` now displays an sstate cache summary (hit/miss counts and percentages) when `--show-sstate` is passed or `layers.show_sstate_summary` is set to `true` via `bakar settings set`
- `bakar report` now displays a buildhistory section (image size, top packages by size, package count, dirty layers) when a `buildhistory` directory is present in the workspace — no flag required, the data's presence is the gate
- `bakar build` now prints the sstate cache summary after a successful build when `layers.show_sstate_summary` is enabled, mirroring the existing `show_hashes` / `--show-layers` behavior
- Added `layers.show_sstate_summary` setting, persistable via `bakar settings set layers.show_sstate_summary true`
- `bakar report` now finds test runs under `build-*/build/runs` (e.g. meta-avocado and custom build-dir BYO workspaces), preventing silent fallback to a stale generic run

### Fixed

- `bakar build --show-layers` (and `show_hashes` config) now correctly prints the layer hash table for bitbake-setup (bbsetup) workspaces; it previously printed nothing
- `bakar build --show-layers` on a fresh BYO build now shows the correct layer table instead of printing nothing (or showing a stale previous build's state), because the table is now printed after `kas` writes `bblayers.conf`
- `bakar build --show-layers --dry-run` on bbsetup workspaces now prints the layer table instead of silently skipping it due to an early exit
- Layer hash table entries containing git short hashes that match scientific-notation patterns (e.g. `25850e97`) are no longer incorrectly highlighted in cyan by Rich
- Enabling the `hashequiv` overlay no longer arms a connection to the public Yocto hash-equivalence server by default; builds in environments without access to that server no longer hang at startup. Set `BB_HASHSERVE_UPSTREAM` in the environment to opt in
- Layer hashes are now correctly resolved for bitbake-setup workspaces (the `layers/<repo>` layout); previously the hash table came back empty for bbsetup builds
- Malformed or unreadable `config-upstream.json` no longer causes the bbsetup workspace detector to propagate an error; it now correctly returns "not a bbsetup workspace"
- A malformed or incomplete `error-report.json` (missing keys or wrong-typed fields) now correctly falls through to the live-parse path in `bakar triage` instead of propagating an exception

## [0.8.0] - 2026-06-03

### Added

- Added `--sstate-mirror` flag to `bakar build` that configures an HTTP sstate/downloads mirror URL and automatically enables the shared-cache overlay (`SSTATE_MIRRORS`, `BB_HASHSERVE_UPSTREAM`) without requiring manual kas YAML edits.
- Added `sstate_mirror_url` field to the `[build]` section of `config.toml`, equivalent to passing `--sstate-mirror` on every build invocation.
- Added a `build_revision` field to report output: a short stable identity hash derived from the current layer checkout SHAs, visible in both human-readable and `--json` report output.
- Added warning and error tallies printed at the end of every build (e.g. "3 warnings, 1 error"), so build noisiness is visible without grepping `kas.log`.
- Added extended triage suggestions for compiler OOM kills (`cc1plus` killed by OOM killer), GitHub API rate limits (HTTP 429), DNS/network failures (with PREMIRROR recommendation), and mirror connection refusals.
- `bakar triage` now discovers run directories across all BSP families and BYO/generic workspaces, not only `nxp/` and `ti/` subdirectories.

### Changed

- `bakar triage` now reads a pre-computed `error-report.json` artifact written at build-failure time instead of re-parsing the full `kas.log` on every invocation, making triage significantly faster for completed failed runs. Run directories produced before this change continue to work via the existing log-parse fallback.
- `bakar clean-cache` sstate pruning now uses a two-phase move-then-delete strategy: stale files are staged inside a `.bakar-gc-<pid>` directory within the sstate root before deletion. This prevents half-deleted state under concurrent builds and leaves a recoverable staging directory if the process is interrupted.
- Timestamped phase headers (step start, success, and failure) are now written to `console.log`, making it possible to identify build step boundaries without cross-referencing `events.jsonl`.

## [0.7.0] - 2026-06-02

### Added

- Added `[build] ccache_shared` and `[build] ccache_dir` settings to share one ccache across all workspaces (cross-BSP hits, less disk) instead of the per-workspace default.
- `clean-cache` now also prunes the ccache via `ccache --evict-older-than`, with `--ccache-dir` and `--sstate`/`--ccache` scoping flags.

### Changed

- Renamed the `clean-sstate` command to `clean-cache`; it now prunes both the sstate cache and the ccache by age. Update any scripts that call `bakar clean-sstate`.

## [0.6.0] - 2026-06-01

### Added

- Added `bakar init` command for interactive workspace creation, guiding new users through BSP family selection and workspace configuration with family-specific prompts for NXP, TI, bbsetup, and generic targets.
- Added non-interactive flags (`--family`, `--workspace`, `--manifest`, `--machine`, `--distro`, `--image`, `--kas-yaml`, `--no-sync`) to `bakar init` for scripted or CI-driven workspace creation without a TTY.
- Added workspace-scoped defaults via `.bakar.toml`: settings written by `bakar init` (machine, distro, image, manifest) are now persisted per-workspace and applied automatically without requiring flags at every invocation.
- Added a new configuration resolution tier so the full precedence chain is: CLI flag > `BAKAR_*` env var > workspace `.bakar.toml` > `~/.config/bakar/config.toml` > built-in default.
- Added documentation for `bakar init`, workspace `.bakar.toml` defaults schema, and the updated five-tier precedence table in the configuration reference.

### Fixed

- Fixed `bakar init` cancelling a prompt (Ctrl-C or Escape) causing an uncaught `TypeError` and leaving a partial workspace directory on disk; prompts now abort cleanly.
- Fixed machine values written by `bakar init` into `[defaults.generic]` being silently ignored when resolving configuration for generic workspaces.
- Fixed a Python exception-handling bug where catching multiple exception types without parentheses silently caught only `ValueError` instead of both `ValueError` and `OSError`.
- Fixed `bakar init` not appearing in the CLI help output (`--help`) or quick-navigation table because its module was never imported at startup.

## [0.5.0] - 2026-05-29

### Breaking

- Renamed the tool from `bspctl` to `bakar`. The console script is now
  `bakar` (the old `bspctl` command is removed with no alias) and the
  importable package is now `bakar`.
- Renamed all `BSPCTL_*` environment variables to `BAKAR_*`. The old
  names are no longer read - there is no fallback.
- Renamed the workspace marker `.bspctl.toml` to `.bakar.toml` and the
  state directory `.bspctl/` to `.bakar/`. Existing workspaces must
  rename these manually before running `bakar`.

### Added
- Added `bakar layers` command to inspect which git revisions back each synced layer without running a full build.
- Added `bakar for-all <cmd>` command to run a shell command across every cloned source repository, exporting `BAKAR_REPO_NAME`, `BAKAR_REPO_PATH`, and `BAKAR_REPO_COMMIT` per invocation; exits non-zero if any repo fails while still visiting all repos.
- Added `bakar settings` subcommand (`list`, `get`, `set`, `unset`) for managing `~/.config/bakar/config.toml` without hand-editing; unknown keys and type mismatches are rejected with a non-zero exit.
- Added `bakar diff <old> <new>` to compare layer SHAs between two NXP/TI manifest XMLs or delegate to `kas diff` for BYO/bbsetup configs.
- Added `bakar prefetch` to run `bitbake --runall=fetch` through the existing kas environment, enabling offline source population without a full build.
- Added `bakar dump` to print or stream the fully resolved kas YAML (after include expansion and overlay merging) to stdout or a file.
- Added `bakar lock` to pin floating layer SHAs: wraps `repo manifest -r` for NXP workspaces and `kas lock` for BYO/bbsetup/TI.
- Added `bakar report [run-id]` to display a post-build summary (image size, duration, layer state) in human-readable or `--json` form, resolving the latest run when no ID is given.
- Added `bakar clean-sstate` for age-based sstate-cache pruning (default 30 days, dry-run by default; `--yes` deletes). Automatically detects `noatime` mounts and falls back to mtime-based pruning with a warning.
- Added `bakar hashserv` subcommand (`start`, `stop`, `status`) for explicit lifecycle control of a persistent per-workspace `bitbake-hashserv` daemon; `bakar build` auto-starts the daemon when `build.hashserv = true` is set in config.
- Added seven new `[build]` config keys accessible via `bakar settings`: `dl_dir`, `sstate_dir`, `sstate_mirrors`, `scheduler`, `pressure_max_cpu`, `pressure_max_io`, and `pressure_max_memory`. Integer keys are stored as integers (not strings) in config.toml.
- Added `--psi-calibrate` flag to `bakar doctor` to sample CPU/IO/memory pressure at 0.5 s intervals and recommend `pressure_max_*` thresholds at peak + 20% headroom.
- Added new `bakar doctor` pre-flight checks: PSI kernel support, git global identity (honoring `includeIf` conditionals), kas YAML syntax, workspace filesystem hardlink safety, Docker version (≥ 20.10) and storage driver (`overlay2`), ccache fill level, and persistent hashserv reachability.
- Added a `bakar-tuning-hashequiv.yml` opt-in overlay enabling OEEquivHash with a local `BB_HASHSERVE`; when `build.hashserv = true` is configured, bakar automatically appends this overlay (deduplicating if the user also passes it explicitly).
- Added per-command documentation under `docs/` and a navigation index at `docs/index.md`; README condensed to a quickstart with a commands table.

### Changed
- Raised minimum Python version to 3.14; Python 3.12 and 3.13 are no longer supported.
- Log output from the Rich console now goes to stderr, keeping stdout clean for commands like `gen-kas` that emit machine-readable text.
- `bakar doctor` git identity check now runs from the workspace directory so `includeIf "gitdir:..."` conditionals are honoured; a false BLOCK no longer fires for developers using per-project git identities.
- `BB_DISKMON_DIRS` in all tuning overlays updated from deprecated `ABORT` keyword to `HALT` (required for Yocto scarthgap and later).
- All tuning overlays now set `BB_HASHSERVE_UPSTREAM = ""` to prevent silent build hangs when the container cannot reach the public Yocto hash equivalence server.
- `bakar clean --all` now gracefully stops the persistent hashserv daemon before wiping the workspace, preventing SQLite WAL corruption.
- Settings config file is now written atomically (temp file + replace) to prevent truncation on crash.
- `for-all` now catches `OSError` from subprocess invocation so a removed or inaccessible repo directory counts as a failure and the loop continues to remaining repos.

### Fixed
- Fixed `bakar report` crashing with `UnboundLocalError` on NXP/TI workspaces due to the `family` variable being read before assignment.
- Fixed `bakar diff` silently treating BYO kas configs as empty NXP manifests; dispatch now keys on file type (`.xml` → structural diff, everything else → `kas diff`).
- Fixed `bakar doctor` kas-yaml-syntax check incorrectly returning FAIL for a valid YAML when the remote branch was rebased; the check now returns SKIP so the subsequent sync step can repair git state.
- Fixed `bakar doctor` kas-yaml-syntax error message showing an irrelevant INFO log line instead of the actual ERROR message.
- Fixed exception handling in the disk-usage sampler that was silently swallowing programmer errors; narrowed to `(SubprocessError, OSError, ValueError)` and logs only the first failure instead of flooding the run log.
- Fixed `triage` not finding run directories for meta-avocado workspaces (builds land in `build-<stem>/build/runs/`).

## [0.4.0] - 2026-05-26

### Added

- Added support for **bitbake-setup workspaces** as a new BSP family (`bbsetup`). `bakar` now auto-detects a bitbake-setup workspace from the current
directory, translates its `config-upstream.json` and `sources-fixed-revisions.json` into a `kas-bbsetup.yml`, and routes `gen-kas`, `build`, `doctor`, and
`triage` subcommands accordingly.
- `bakar gen-kas` regenerates `kas-bbsetup.yml` from a bitbake-setup workspace's resolved configuration, pinning each layer repository to its fixed-revision
SHA.
- `bakar build` now runs the kas pipeline on bitbake-setup workspaces using the generic tuning overlay without requiring a manifest file or YAML argument.
- `bakar doctor` now runs dedicated pre-flight checks for bitbake-setup workspaces, verifying that the workspace is initialized and that sources are present.
- `bakar sync` on a bitbake-setup workspace now fails fast with guidance to use `bitbake-setup init` instead of silently attempting an unsupported sync.
- Generated and committed kas YAML files now declare configuration format version 21 (up from 3), compatible with kas 4.x and newer.

### Fixed

- Fixed a build failure that could occur when kas attempted to verify a pinned commit's reachability against a branch that had moved forward; commit-pinned
repos in the `bbsetup` kas translation now emit only the SHA, omitting the branch anchor.

## [0.3.0] - 2026-05-25

### Added

- Persistent user configuration via `~/.config/bakar/config.toml` — set default machine, distro, image, manifest, repo URL, and container image without exporting environment variables on every shell session. An absent file falls back to built-in defaults and is never auto-created.
- `examples/config.toml` reference file with every available key commented out and annotated; copying it to `~/.config/bakar/config.toml` is inert until a key is uncommented.
- `--show-layers` flag and `show_hashes` config key to print each layer's git short hash and branch after a build, mirroring what bitbake logs. The layers table now also includes the bitbake version.
- Layer hash collection now supports generic kas YAML builds that use `${TOPDIR}`-relative layer paths (e.g. `${TOPDIR}/../layers/<repo>`), in addition to the existing NXP/TI `/sources/`-based layout.
- `doctor` config key to suppress the pre-flight doctor check without passing `--skip-doctor` on every invocation.

### Changed

- Configuration resolution now follows a four-tier precedence chain: CLI flag → environment variable → `config.toml` → built-in default. Setting `container_image` in `config.toml` activates container mode (same behaviour as `KAS_CONTAINER_IMAGE`) and prints a notice so the switch is not silent.
- The container-bitbake doctor check now bind-mounts the workspace bitbake directory into the container when available, producing a real version string instead of an opaque "inspection failed" / SKIP result. When `which bitbake` returns a not-found message the check now reports "not in container PATH (workspace-sourced)" rather than a generic failure.
- A config file parse error now exits with code 2 and prints the file path, making misconfigured `config.toml` files immediately identifiable.

## [0.2.1] - 2026-05-23

### Fixed
- `BB_NUMBER_THREADS` and `PARALLEL_MAKE` defaulted to 16 regardless of the host's CPU count. `_build_env()` now sets `NPROC` to `os.cpu_count()` before invoking kas, so the tuning overlay picks up the actual core count. Set `NPROC` explicitly to override.
- `bakar doctor` now reports the effective `NPROC` value at pre-flight time via the new `nproc` INFO check.

## [0.2.0]

### Fixed
- Overlay YAMLs were missing from the published wheel: `overlays/` at repo root is not picked up by `uv_build`. Moved to `src/bakar/overlays/` so the files are included as package data. Every prior release was broken - `bakar build` raised `FileNotFoundError` on the first run.
- `_overlay_dir()` walked `__file__` three levels up to the repo root, producing a non-existent path under `site-packages/`. Replaced with `importlib.resources.files("bakar") / "overlays"`.

## [0.1.0] - 2026-05-22

### Added
- `bakar build --host` and `bakar shell --host` flags bypass `kas-container` and run plain `kas`/`kas shell` directly on the host - no Docker required.
- Auto-detection: when `KAS_CONTAINER_IMAGE` is absent from the environment, host mode activates automatically. Set the variable to opt into container builds.
- Example kas YAML (`examples/kas-qemux86-64-wrynose.yml`) for a local, network-free wrynose (Yocto master) minimal build on qemux86-64 using repos from `~/repos/personal/yocto/`.

### Changed
- Releases are now driven by `scripts/release.sh`, which enforces an atomic bump+push (preconditions and validation gates run first, then `bump-my-version` and `git push --follow-tags` execute back-to-back with no opportunity to interleave commits).

## [0.0.3] - 2026-05-22

### Added
- First release published to PyPI. Install with `uv tool install bakar` or `pip install bakar`.
- Python 3.14 added to the supported version matrix (3.11–3.14).
- GitHub Actions CI workflow: test matrix across Python 3.11–3.14, ruff lint, ty type-check.
- Automated PyPI publishing via OIDC Trusted Publisher on version-tag push, with a GitHub release
  created from the matching CHANGELOG section.
- `RELEASING.md`: step-by-step release guide covering PyPI Trusted Publisher setup and the
  `bump-my-version` workflow.

### Changed
- Documentation leads with kas wrapper identity (four general capabilities); NXP/TI vendor
  manifest support is now presented as a secondary layer built on top.

## [0.0.2] - 2026-05-21

## [0.0.1] - 2026-05-21

### Added
- Initial public release.
- NXP i.MX BSP support via Google `repo` + `var-setup-release.sh` + `kas-container`.
- TI Sitara BSP support via `varigit/oe-layersetup` + `kas-container`.
- Generic BYO kas YAML support for any non-NXP/TI build.
- Pre-flight `bakar doctor` checks with BLOCK/WARN/INFO severity.
- Structured per-run observability under `<bsp_root>/build/runs/<ts>/` (events.jsonl, console.log, kas.log, env.txt, time.log, du.tsv).
- `bakar triage` post-mortem with keyed failure-pattern suggestions.
- Vendor config layer at `~/.config/bakar/vendors.toml` for custom board families.

[Unreleased]: https://github.com/jetm/bakar/compare/v0.22.0...HEAD
[0.22.0]: https://github.com/jetm/bakar/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/jetm/bakar/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/jetm/bakar/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/jetm/bakar/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/jetm/bakar/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/jetm/bakar/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/jetm/bakar/compare/v0.15.1...v0.16.0
[0.15.1]: https://github.com/jetm/bakar/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/jetm/bakar/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/jetm/bakar/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/jetm/bakar/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/jetm/bakar/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/jetm/bakar/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/jetm/bakar/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/jetm/bakar/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/jetm/bakar/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/jetm/bakar/compare/v0.4.0...v0.7.0
[0.4.0]: https://github.com/jetm/bakar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jetm/bakar/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jetm/bakar/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jetm/bakar/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jetm/bakar/compare/v0.0.3...v0.1.0
[0.0.3]: https://github.com/jetm/bakar/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/jetm/bakar/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/jetm/bakar/releases/tag/v0.0.1
