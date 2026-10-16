# Configuration Reference

Complete reference for all bakar configuration options across the three config
files. For the resolution order and an introduction, see
[configuration.md](configuration.md). To read and write `config.toml` via the
CLI, see [settings.md](settings.md).

## Resolution order (highest priority first)

1. CLI flag (`-m`, `-f`, `--sstate-mirror`, ...)
2. `BAKAR_*` environment variable
3. Workspace `.bakar.toml` (`[defaults.<family>]`)
4. Preset selected with `--preset` (`machine`, `distro`, `image`, `manifest`)
5. User `~/.config/bakar/config.toml`
6. Built-in BSP default

The preset tier applies only to the four target fields named above. The
`[build]` toggles resolve on their own tiers; see [Build toggles](#build-toggles).

---

## `~/.config/bakar/config.toml`

Managed with `bakar settings list/get/set/unset`. Editable directly as TOML.
Writes are atomic; a crash mid-write leaves the previous file intact.
Unknown keys are rejected by `bakar settings set`. When the file is loaded, an
unknown key inside a known section and an unknown top-level section each emit
one warning that names the offender and the recognized names, and are then
ignored, so a key added by a newer bakar does not break an older one. A known
section that is not a table (for example `build = "x"`) and a value of the wrong
type (for example `scheduler = 5`) raise an error that names the file and the
section or key, and the load stops.

### `[defaults.nxp]` - NXP i.MX default targets

| Key | Type | Built-in default | Description |
|-----|------|-----------------|-------------|
| `machine` | string | `imx8mp-var-dart` | Default `MACHINE` for NXP builds. Overridden by `--machine`/`-m` or `BAKAR_MACHINE`. |
| `distro` | string | `fsl-imx-xwayland` | Default `DISTRO`. Overridden by `--distro`/`-d` or `BAKAR_DISTRO`. |
| `image` | string | `core-image-minimal` | Default bitbake image target. Overridden by `--image`/`-i` or `BAKAR_IMAGE`. |
| `manifest` | string | `imx-6.6.52-2.2.2.xml` | Default repo manifest filename. Overridden by `--manifest`/`-f` or `BAKAR_MANIFEST`. |
| `repo_url` | string | `https://github.com/varigit/variscite-bsp-platform.git` | Override the variscite-bsp-platform manifest repo URL. Overridden by `BAKAR_REPO_URL`. |

### `[defaults.ti]` - TI Sitara default targets

| Key | Type | Built-in default | Description |
|-----|------|-----------------|-------------|
| `machine` | string | `am62x-var-som` | Default `MACHINE` for TI builds. Overridden by `--machine`/`-m` or `BAKAR_MACHINE`. |
| `distro` | string | `arago` | Default `DISTRO`. Overridden by `--distro`/`-d` or `BAKAR_DISTRO`. |
| `image` | string | `var-thin-image` | Default image target. Overridden by `--image`/`-i` or `BAKAR_IMAGE`. |
| `manifest` | string | *(long Processor SDK filename)* | Default oe-layertool config filename. Overridden by `--manifest`/`-f` or `BAKAR_MANIFEST`. |

### `[build]` - build pipeline behaviour

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `show_doctor_report` | bool | `true` | Show the pre-flight doctor report before every build and sync. Set to `false` to print only build-blocking issues; the global `--hide-doctor-report` flag does the same for one invocation. Doctor checks always run - a BLOCK-severity failure aborts regardless. |
| `container` | bool | `false` | Route builds through `kas-container` instead of running `kas` on the host. Host execution is the default; this is the opt-in. Overridden by a workspace `.bakar.toml` `[build]` value, by `BAKAR_CONTAINER`, and by the global `--container` flag. |
| `host_mode` | bool | `false` | Accepted and ignored. The key still parses so existing configs load, but it does not select host execution or anything else: host is already the default and `container` is the only opt-in away from it. |
| `kas_container_image` | string | `jetm/kas-build-env:latest` | kas-container image tag. Names the image used once the container path is opted into via `container`; setting it does not select the container. Overridden by a workspace `.bakar.toml` `[build]` value and by the `KAS_CONTAINER_IMAGE` env var. |
| `dl_dir` | string | *(not set)* | Override `DL_DIR` (shared download cache path). Passed to kas-container as an env var. |
| `sstate_dir` | string | *(not set)* | Override `SSTATE_DIR` (sstate cache path). Passed to kas-container as an env var. |
| `sstate_mirrors` | string | *(not set)* | Raw `SSTATE_MIRRORS` value passed to the build. Use `sstate_mirror_url` unless you need full control over the mirror syntax. |
| `sstate_mirror_url` | string | *(not set)* | HTTP(S) sstate/downloads mirror URL. When set, bakar activates the shared-cache overlay (`bakar-tuning-shared-cache.yml`), which wires `SSTATE_MIRRORS` to `<url>/all/PATH;downloadfilename=PATH` and sets `BB_HASHSERVE_UPSTREAM`. Equivalent to passing `--sstate-mirror` on every invocation. The Yocto Project public mirror (`http://sstate.yoctoproject.org`) works without any additional config. |
| `scheduler` | string | *(not set)* | BitBake task scheduler: `speed` (maximize parallelism) or `completion` (minimize task switches). |
| `pressure_max_cpu` | float | *(not set)* | PSI cpu avg10 pressure threshold (0-100). Exported to the build as `BB_PRESSURE_MAX_CPU` converted to bitbake's stall-microseconds-per-second scale (percent × 10,000); bitbake stops launching new tasks while the measured stall rate exceeds it. Must be `>= 1`. Generate calibrated values with `psi_autocalibrate`. |
| `pressure_max_io` | float | *(not set)* | PSI io avg10 threshold. Same semantics as `pressure_max_cpu`. |
| `pressure_max_memory` | float | *(not set)* | PSI memory avg10 threshold. Same semantics as `pressure_max_cpu`. |
| `psi_autocalibrate` | bool | `false` | When `true`, bakar samples `/proc/pressure` during each build and writes updated `pressure_max_*` values back to this file after completion. The first build bootstraps the values; subsequent builds only raise a threshold (from an unthrottled measurement), never lower it, so a light sstate-cached build cannot over-throttle the next cold one. Delete the `pressure_max_*` keys to recalibrate from scratch. |
| `disk_free_threshold_gb` | float | `50.0` | Minimum free disk space (GB) enforced by the doctor `disk-free` check before each build. Must be `> 0`. |
| `stall_abort_secs` | int | `2700` | Self-guard against a wedged task (e.g. a deadlocked final link). When every running task's log has been silent this many seconds, bakar SIGINTs the build so it fails cleanly with a `stall-timeout` step naming the stuck task, instead of spinning until you `Ctrl-C`. The signal is per-task log freshness, not raw output (bitbake's own keepalive lines would otherwise mask a hang). Set to `0` to disable. Must be `>= 0`. |
| `stop_on_error` | bool | `true` | SIGINT the build the moment any task fails, rather than waiting for bitbake's own halt-on-failure to drain every already-running task on its own schedule (which can take a long time - an unrelated `do_compile` can still be running when the failure landed). bitbake already stops scheduling *new* tasks on first failure regardless of this setting; this only stops bakar from rendering a misleadingly-normal live view while it waits. Set to `false` to fall back to waiting for the natural drain. |
| `capture_graph` | bool | `true` | Capture the dependency graph (`bitbake -g`) into the run directory after a build that succeeded, so `bakar graph` and the critical-path analyses have something to read. Set to `false` to decline it. Declining returns before the cooker-idle wait the capture needs, so it costs no waiting at the end of a build - useful on a machine where the extra bitbake pass is unwanted. Opt out per-invocation with `bakar build --no-capture-graph`. |
| `scope` | bool | `true` | Run the build inside a transient `systemd-run --user --scope` so it survives terminal/session teardown (SSH disconnect, closed shell). By default the scope only applies session-survival plus a positive `oom_score_adjust` and below-default CPU/IO weights - it imposes **no** memory ceiling, so memory behaves as it did before the scope. Hard cgroup memory caps are opt-in via the `scope_memory_*` keys below (off by default; harmful on a host with a large zram swap). No-op when `systemd-run`/the user manager is unavailable. Opt out per-invocation with the global `--no-scope`. **Does not** touch `BB_NUMBER_THREADS`/`PARALLEL_MAKE`. |
| `scope_memory_high` | float | `0.0` | Fraction of physical RAM for the scope's `MemoryHigh` soft reclaim cushion. **Off by default** (`0.0` = omit). Only meaningful **paired with `scope_memory_max`** - it is ignored (with a warning) when `scope_memory_max` is unset, because `MemoryHigh` alone (swap still available) just makes a zram host reclaim-and-swap-thrash. With the cap set, `MemoryHigh` is a slow-crawl band below it; prefer `scope_memory_max` alone, or set `high == max`. Range `[0, 1]` (0 disables; a fraction under 1% rounds to 0% and is treated as off). |
| `scope_memory_max` | float | `0.0` | Fraction of physical RAM for the scope's `MemoryMax` hard ceiling. **Off by default** (`0.0` = omit): on a host with a large zram/zswap swap the ceiling backfires (a cap without a swap deny is defeated by zram spill; a cap with the paired swap deny leaves the build's anon unswappable and reclaim spins), so a build that fit in RAM before the scope never needs it. Enable only on a *dedicated* build host where OOM-killing the build to protect the host is wanted, and **size it above your largest single link step** (e.g. chromium's final `ld`) or that step OOM-kills. When set, the scope also emits `MemorySwapMax=0` so the cap is a real RAM ceiling (a clean cgroup-OOM, visible in `journalctl --user -u <unit>`); `scope_memory_max = 1.0` gives swap-denied-at-100% if you want swap off without a sub-total cap. Range `[0, 1]` (0 disables; under 1% rounds to 0% = off). |
| `scope_oom_score_adjust` | int | `500` | Positive `oom_score_adj` applied to the build (via an inherited value written before exec, since a scope unit cannot carry `OOMScoreAdjust`). Under *global* memory pressure the kernel then picks the build as the OOM victim and protects system services. Range `[0, 1000]`; `0` disables the adjustment. |
| `scope_cpu_weight` | int | `0` | `CPUWeight` for the scope. **Off by default (`0` omits the property).** Setting it makes systemd realize the cpu cgroup controller across the whole `app.slice` hierarchy and its siblings, not just this scope. Range `[0, 10000]`. |
| `scope_io_weight` | int | `0` | `IOWeight` for the scope. **Off by default (`0` omits the property).** Setting it realizes the io controller session-wide; under heavy build I/O the io controller's proportional throttling can cause a session-wide priority-inversion stall (a hang with no OOM and no panic - observed with chromium). Leave at `0` unless you have measured a need. Range `[0, 10000]`. |
| `journal` | bool | `true` | Emit structured milestone records (`build_start`, `progress`, `task_failed`, `build_end`) to the systemd journal, carrying build progress plus host and cache health. Build **output** is never sent - it stays in `kas.log` - because journald rate-limits to 10,000 messages per 30s by default, drops the excess silently, and a scope unit cannot raise that limit. Turn off on a host whose journal is already at its `SystemMaxUse` cap and evicting. See [build.md](build.md#journal-milestone-records). |
| `journal_interval` | int | `300` | Seconds between `progress` snapshots. Level-sampled on a fixed tick, so a build that stopped moving shows as an unchanged record rather than as silence. Lower it for short builds you want finer resolution on; each tick is one journal record. |
| `hashserv` | bool | `false` | Persistent workspace-scoped `bitbake-hashserv` daemon. When `true`, bakar starts a hashserv instance on the first build and reuses it on subsequent ones, accumulating `OEEquivHash` sstate equivalence across builds. See [hashserv.md](hashserv.md). |
| `ccache_shared` | bool | `false` | Share one ccache across all workspaces instead of a per-workspace cache. Default shared path: `~/.cache/bakar/ccache`. Cross-BSP compiler cache hits become possible. Note: a shared cache is subject to its own size cap (`CCACHE_MAXSIZE` from the tuning overlay, default 50 GB); raise it with `export CCACHE_MAXSIZE=100G` when sharing across many workspaces. |
| `ccache_dir` | string | *(not set)* | Explicit ccache directory. Takes precedence over `ccache_shared` and the per-workspace default. Useful for pointing several machines at a shared NFS-mounted cache. |
| `feed_shared` | bool | `false` | Render the local package feed into one location shared by every workspace instead of a per-workspace `<workspace>/_feed`. Default shared path: `~/.local/share/bakar/feed` - the XDG *data* home, not the cache home, because a served feed must not be evicted under a client that is mid-resolve. Sharing matters for a multi-machine feed: the release-global toolchain repository only lists the union of every machine when they render into a common root. |
| `feed_dir` | string | *(not set)* | Explicit package feed root. Takes precedence over `feed_shared` and the per-workspace default. This is the key a real deployment uses: the feed grows on every build and is expensive to re-render, so it usually belongs on a large volume rather than wherever the workspace or data home happens to sit. |
| `ccache` | bool | `false` unless `sccache_dist` is on | Enable the ccache local-object-cache overlay. When unset, follows `sccache_dist` - on when it's on, off otherwise - so non-allowlisted recipes keep a compiler cache under sccache-dist; set explicitly (`true`/`false`) to override either way. The `sccache.bbclass` allowlist keeps the two from double-wrapping the same recipe's `CC`. |
| `sccache_dist` | bool | `false` | Route C/C++ `do_compile` through an sccache-dist cluster. Requires `sccache_scheduler_url` (or the global `--sccache-scheduler`). See [sccache-dist.md](sccache-dist.md). |
| `sccache_scheduler_url` | string | *(not set)* | sccache-dist scheduler URL, e.g. `http://localhost:10600`. Overridden by the global `--sccache-scheduler` flag. |
| `rm_work` | bool | `false` | Strip each recipe's WORKDIR after it builds. Off by default: while bakar is in use the tuning stack keeps work dirs so `inspect`/`diffsigs`/devshell still work. |
| `nproc` | int | *(auto: `os.cpu_count()`)* | Base build parallelism, exported as the `NPROC` env var. The tuning overlays fan it out to `BB_NUMBER_THREADS`, `PARALLEL_MAKE`, and `BB_NUMBER_PARSE_THREADS` unless `parallel_make` / `bb_number_threads` override them. A live `NPROC=` env var still wins over this value. Must be `> 0`. |
| `parallel_make` | int | *(falls back to `nproc`)* | Compile `-j` (`PARALLEL_MAKE`), decoupled from recipe concurrency. Size it to the total cores available to compilation - for an sccache-dist cluster that is the sum across all build servers, so compile jobs spill to remote nodes. Exported as `BAKAR_PARALLEL_MAKE`. Must be `> 0`. |
| `bb_number_threads` | int | *(falls back to `nproc`)* | Recipe concurrency (`BB_NUMBER_THREADS`; `BB_NUMBER_PARSE_THREADS` follows it), decoupled from compile `-j`. Size it to local RAM - too many parallel recipes OOM a full image build. Exported as `BAKAR_BB_NUMBER_THREADS`. Must be `> 0`. |
| `worktree_build_dir` | string | `build-{stem}-{worktree}` | Build directory name for a kas file in a linked meta-avocado worktree. Only `{stem}` (the kas file's stem) and `{worktree}` (the worktree directory name) are allowed, with no format spec or conversion, and the rendered name must be one directory name (no `/`, `.` or `..`). A main checkout keeps `build-{stem}`. Overridden by `BAKAR_WORKTREE_BUILD_DIR`. |
| `local_tmpdir_base` | string | *(not set)* | Node-local base directory for the build `TMPDIR`. When set and the build runs in host mode, the build tmp moves off the (possibly NFS) source workspace to a directory under this base on local disk, named from the workspace directory and the machine. Unset keeps the workspace-relative tmp. Ignored in container mode. |
| `stop_grace_seconds` | int | `30` | How long `bakar stop` waits after the graceful SIGINT before escalating to SIGTERM and then SIGKILL. `0` restores the unbounded wait. Must be `>= 0`. Overridden per invocation by `bakar stop --timeout`. |
| `show_baseline_drift` | bool | `false` | The live build UI loads the persisted per-task timing baselines and colors tasks that overrun their historical mean. Off by default so a checkout with no baseline history shows no misleading drift. Resolves `BAKAR_SHOW_BASELINE_DRIFT`, then the workspace `[build]` value, then this key. |
| `mold` | bool | `false` | Enable the mold linker: adds the `meta-bakar-mold` layer and inherits `mold.bbclass`, which injects `-fuse-ld=mold` into target link steps. Resolves the CLI `--mold` / `--mold-global` flags, then `BAKAR_MOLD`, then this key. |
| `mold_mode` | string | `list` | Which recipes mold applies to: `list` (allow-list through `MOLD_INCLUDED_PN`), `global` (deny-list through `MOLD_EXCLUDED_PN`), `baseline` (`-fuse-ld=bfd` over the allow-list, for a symmetric measurement) or `baseline-global` (the bfd arm at deny-list scope). Any other value fails when the build resolves its config. The CLI flags `--mold-baseline` and `--mold-global` override it. |
| `mold_extra_excluded_pn` | string | *(not set)* | Space-separated recipe names appended to `MOLD_EXCLUDED_PN`. Applies to the deny-list scope only (`global`, `baseline-global`); records a recipe that breaks under global mold without editing `mold.bbclass`. |
| `uninative` | bool | `false` | Use the host-provided uninative tarball. Takes effect only in host mode on an Arch-family host with the `yocto-uninative-tarball` fragment installed; elsewhere it adds nothing. Enabling it changes `NATIVELSBSTRING` to `universal` and so changes sstate signatures, which is why it is an explicit opt-in. Resolves `BAKAR_UNINATIVE`, then this key. |
| `buildtools_dir` | string | *(not set)* | Persisted location of the buildtools-extended toolchain installed by `bakar setup`. Host builds read it after `BAKAR_BUILDTOOLS_DIR`, so the toolchain survives into a new shell. It serves callers that do not name a Yocto release (non-oe-core BSP families). A build that does name one reads only the per-release `[build.buildtools_dirs]` table (release codename to directory, edited by hand, not listed by `bakar settings`) and never falls back to this key. |
| `cluster` | bool | `false` | Explicit cluster-mode opt-in. It is the one gate for the cluster doctor preflight checks (central hashserv and prserv liveness, shared-mount validation); a default single-node build never probes cluster infrastructure. Setting `bb_hashserve` or `prserv_host` alone does not turn it on. Resolves `BAKAR_CLUSTER`, then this key. |
| `cluster_bind_host` | string | *(not set)* | Address the workspace `bitbake-hashserv` and prserv daemons bind to. Unset binds `localhost` only. Set it to the node's cluster-reachable IP so other nodes can share one daemon. |
| `bb_hashserve` | string | *(not set)* | `host:port` of a central hash-equivalence server. When set, the build exports it as `BB_HASHSERVE` and does not start the per-workspace daemon, even with `hashserv = true`. |
| `prserv_host` | string | *(not set)* | `host:port` of a central PR service. When set, the build exports it as `PRSERV_HOST` and does not start the per-workspace prserv. |

### `[workspaces]` - release workspaces for meta-avocado

A meta-avocado kas build is built in a per-release workspace chosen from the
release its pins name (the `openembedded-core` branch with `-avocado` removed).
Nothing is routed until one of these is set. The mapping is read from this file
only.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `template` | string | *(not set)* | Absolute path with a `{release}` placeholder, for example `/home/me/repos/work/peridio-{release}`. `~` is expanded. Any other placeholder, a format spec (`{release:>8}`), a conversion (`{release!r}`) or a relative path is a configuration error. |

`[workspaces.releases]` maps single releases and wins over `template`:

```toml
[workspaces.releases]
scarthgap = "/mnt/old/peridio-scarthgap"
```

Each value is an absolute path; an empty or relative value is a configuration
error. `bakar settings` does not list this table; edit `config.toml` directly.

### `[dispatch]` - `bakar build --on` for meta-avocado

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `worktree_root` | string | `<workspace>/.bakar/worktrees` | Absolute path on the REMOTE host where the delivered worktrees are placed. `~` and relative paths are refused: `~` would expand to the local home, and a relative path would place the worktree inside the shared `meta-avocado` clone. |
| `snapshot_untracked_cap_mb` | int | `50` | Largest amount of untracked content, in MiB, a dispatch snapshot may carry. Must be `> 0`. |

### `[host]` - doctor host-environment thresholds

Floors and ceilings the `bakar doctor` host-environment checks compare live
system state against (`check_sysctl`, `check_docker_ulimits`, `check_memory`).
Each default equals the value doctor previously hardcoded, so an absent `[host]`
table yields verdicts identical to the pre-config behaviour. Precedence:
workspace `.bakar.toml` `[host]` > user `config.toml` `[host]` > built-in floor.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `inotify_instances` | int | `4096` | Floor for the `fs.inotify.max_user_instances` sysctl. Must be `> 0`. |
| `inotify_watches` | int | `524288` | Floor for the `fs.inotify.max_user_watches` sysctl. Must be `> 0`. |
| `swappiness_max` | int | `20` | Ceiling for the `vm.swappiness` sysctl (the check fails when the live value exceeds this). Must be `> 0`. |
| `nofile_soft` | int | `8192` | Floor for the Docker daemon's `default-ulimits` `nofile` soft limit. Must be `> 0`. |
| `mem_min_gb` | float | `16.0` | Minimum available-plus-swap memory floor in GB. Must be `> 0`. |

### `[layers]` - display preferences

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `show_hashes` | bool | `false` | Always print layer git hashes, branches, and build status after every build or sync. Equivalent to running `bakar layers` after each build. |
| `show_sstate_summary` | bool | `false` | Always show the sstate cache summary (task-hit vs miss breakdown) after every build. |

### Complete example

```toml
[defaults.nxp]
machine = "imx8mp-var-dart"
distro = "fsl-imx-xwayland"
image = "var-thin-image"
manifest = "imx-6.12.49-2.2.0.xml"
# repo_url = "https://github.com/MyOrg/my-bsp-platform.git"

[defaults.ti]
machine = "am62x-var-som"
manifest = "processor-sdk-10.1.0.8-config_var1.txt"

[build]
show_doctor_report = true
dl_dir = "/mnt/yocto-cache/downloads"
sstate_dir = "/mnt/yocto-cache/sstate"
sstate_mirror_url = "https://cache.example.com"
# scheduler = "completion"
pressure_max_cpu = 72.0
pressure_max_io = 41.0
pressure_max_memory = 20.0
psi_autocalibrate = true
disk_free_threshold_gb = 50.0
hashserv = true
ccache_shared = true
# ccache_dir = "/mnt/yocto-cache/ccache"
# worktree_build_dir = "build-{stem}-{worktree}"

[workspaces]
template = "/home/me/repos/work/peridio-{release}"

# [workspaces.releases]
# scarthgap = "/mnt/old/peridio-scarthgap"

[dispatch]
# worktree_root = "/home/me/repos/work/peridio-wrynose/.bakar/worktrees"
snapshot_untracked_cap_mb = 50

[host]
inotify_instances = 4096
inotify_watches = 524288
swappiness_max = 20
nofile_soft = 8192
mem_min_gb = 16.0

[layers]
show_hashes = true
show_sstate_summary = false
```

---

## Workspace `.bakar.toml`

Per-workspace defaults written by `bakar init`. Lives at the workspace root
(the directory containing `nxp/`, `ti/`, or the `.bakar.toml` marker file).

Priority: below `BAKAR_*` env vars, above `~/.config/bakar/config.toml`.
The file is loaded by the same rules as `config.toml`. An unrecognized key under
a known section and an unrecognized top-level section each emit one warning
naming the offender and the recognized names, and are then ignored. A known
section (`[defaults]`, `[defaults.<family>]`, `[build]`, `[host]`) that is not a
table, or a value of the wrong type, fails the load with an error that names the
file.

### `[defaults.nxp]`

| Key | Type | Description |
|-----|------|-------------|
| `manifest` | string | Workspace-specific default NXP manifest filename. |
| `machine` | string | Workspace-specific default MACHINE. |
| `distro` | string | Workspace-specific default DISTRO. |
| `image` | string | Workspace-specific default image target. |

### `[defaults.ti]`

| Key | Type | Description |
|-----|------|-------------|
| `manifest` | string | Workspace-specific default TI manifest filename. |
| `machine` | string | Workspace-specific default MACHINE. |
| `distro` | string | Workspace-specific default DISTRO. |
| `image` | string | Workspace-specific default image target. |

### `[defaults.generic]`

| Key | Type | Description |
|-----|------|-------------|
| `kas_yaml` | string | Default kas YAML path for BYO/generic workspaces. |
| `machine` | string | Default MACHINE for BYO/generic builds. |

### `[build]`

A top-level table (not under `[defaults]`) that overrides the user
`config.toml` `[build]` value for this workspace. Precedence: workspace
`.bakar.toml` `[build]` > user `config.toml` `[build]` > built-in default; the
`KAS_CONTAINER_IMAGE` env var still beats all three.

| Key | Type | Description |
|-----|------|-------------|
| `kas_container_image` | string | Workspace override for the kas-container image tag. Names the image used once the container path is opted into; it does not select the container on its own. |
| `container` | bool | Workspace opt-in to `kas-container`. Beaten by `BAKAR_CONTAINER` and the `--container` flag; beats the user `[build] container`. |
| `ccache` | bool | Workspace override for the ccache overlay. Beaten by `BAKAR_CCACHE`; beats the user `[build] ccache`. |
| `rm_work` | bool | Workspace override for stripping each recipe's WORKDIR. Beaten by `BAKAR_RM_WORK`; beats the user `[build] rm_work`. |
| `show_baseline_drift` | bool | Workspace override for the live-UI baseline drift coloring. Beaten by `BAKAR_SHOW_BASELINE_DRIFT`; beats the user `[build] show_baseline_drift`. |
| `host_mode` | bool | Accepted and ignored, as in the user config. Host execution is the default and this key changes nothing. |

### `[host]`

A top-level table (not under `[defaults]`) that overrides the user
`config.toml` `[host]` thresholds for this workspace. Same keys, types, and
defaults as the user-config `[host]` section above; precedence is workspace
`.bakar.toml` `[host]` > user `config.toml` `[host]` > built-in floor.

| Key | Type | Description |
|-----|------|-------------|
| `inotify_instances` | int | Workspace override for the `fs.inotify.max_user_instances` floor. |
| `inotify_watches` | int | Workspace override for the `fs.inotify.max_user_watches` floor. |
| `swappiness_max` | int | Workspace override for the `vm.swappiness` ceiling. |
| `nofile_soft` | int | Workspace override for the Docker `default-ulimits` `nofile` soft floor. |
| `mem_min_gb` | float | Workspace override for the minimum memory floor in GB. |

### Example

```toml
# bakar workspace root
[defaults.nxp]
machine = "imx8mp-var-dart"
manifest = "imx-6.12.49-2.2.0.xml"
```

---

## `~/.config/bakar/vendors.toml`

Custom board families that extend a built-in BSP preset. Vendor entries are
checked before built-in regexes: the first entry whose `manifest_regex` matches
the manifest filename wins.

Edited by hand - there is no `bakar settings` integration for this file.
An absent file produces an empty vendor list.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Identifier shown in logs and error messages. |
| `family` | string | yes | Base BSP family to inherit from: `nxp`, `ti`, `qcom`, `generic`, or `bbsetup`. |
| `manifest_regex` | string | yes | Python regex matched against the manifest filename (not the full path). Max 200 characters. Must compile. |
| `repo_url` | string | no | Override the repo manifest URL for this vendor. |
| `kas_container_image` | string | no | Override the kas-container image for this vendor. |
| `default_machine` | string | no | Default MACHINE when none is specified. |
| `default_distro` | string | no | Default DISTRO. |
| `default_image` | string | no | Default image target. |
| `default_manifest` | string | no | Default manifest filename. |
| `default_branch` | string | no | Default repo branch. |
| `branch_by_manifest_prefix` | `{string: string}` | no | Map manifest filename prefix → repo branch. Lets one vendor entry cover multiple release trains. |
| `tuning_overlay` | string | no | Tuning overlay name or path appended to every build for this vendor. |

### Example

```toml
[[vendors]]
name           = "my-board"
family         = "nxp"
manifest_regex = "my-board-.*\\.xml"

default_machine  = "my-imx8mp-board"
default_distro   = "fsl-imx-xwayland"
default_image    = "my-custom-image"
default_manifest = "my-board-6.12.0.xml"
default_branch   = "lf-6.12.y-var01"
repo_url         = "https://github.com/MyOrg/bsp-platform.git"

# Optional: different branch per manifest prefix
# branch_by_manifest_prefix = {"my-board-6.6" = "lf-6.6.y", "my-board-6.12" = "lf-6.12.y"}
```

---

## `BAKAR_*` environment variables

Override config.toml and workspace defaults for one invocation. CLI flags
still take precedence over env vars.

### Build target overrides

| Variable | Overrides | Example |
|----------|-----------|---------|
| `BAKAR_MACHINE` | `--machine`, `defaults.*.machine` | `BAKAR_MACHINE=imx8mp-var-dart bakar build -f manifest.xml` |
| `BAKAR_DISTRO` | `--distro`, `defaults.*.distro` | `BAKAR_DISTRO=fsl-imx-xwayland` |
| `BAKAR_IMAGE` | `--image`, `defaults.*.image` | `BAKAR_IMAGE=core-image-base` |
| `BAKAR_MANIFEST` | `--manifest`, `defaults.*.manifest` | `BAKAR_MANIFEST=imx-6.12.49-2.2.0.xml` |
| `BAKAR_REPO_BRANCH` | `--branch`, repo branch | `BAKAR_REPO_BRANCH=lf-6.12.y-var01` |
| `BAKAR_REPO_URL` | `defaults.nxp.repo_url` | `BAKAR_REPO_URL=https://github.com/MyOrg/bsp.git` |

### Container

| Variable | Description |
|----------|-------------|
| `BAKAR_CONTAINER` | Set to `1` to route the build through `kas-container` for one invocation. Host execution is the default; this is the opt-in, equivalent to the global `--container` flag. Beats the workspace and user `[build] container` toggles. |
| `BAKAR_HOST_MODE` | Accepted and ignored: bakar does not read this variable. Host execution is the default; the only opt-in away from it is `BAKAR_CONTAINER`, `--container` or a `[build] container` toggle. |
| `KAS_CONTAINER_IMAGE` | Override the kas-container image for one invocation. Takes precedence over both the workspace `.bakar.toml` `[build]` value and `build.kas_container_image` in the user config. Names the image only; it does not select the container path. |

### Build toggles

Each variable toggles one `[build]` boolean for one invocation. A set,
non-blank value is true when it is `1`, `true`, `yes` or `on` (any case) and
false for anything else; an unset or blank variable is skipped and the next
tier decides. `BAKAR_CONTAINER` is parsed the same way.

| Variable | `[build]` key | Resolution (highest first) |
|----------|---------------|----------------------------|
| `BAKAR_MOLD` | `mold` | CLI `--mold` / `--mold-global`, env, user config, default off. No workspace tier. |
| `BAKAR_UNINATIVE` | `uninative` | env, user config, default off. No workspace tier. |
| `BAKAR_CLUSTER` | `cluster` | env, user config, default off. No workspace tier. |
| `BAKAR_RM_WORK` | `rm_work` | env, workspace `[build]`, user config, default off. |
| `BAKAR_SHOW_BASELINE_DRIFT` | `show_baseline_drift` | env, workspace `[build]`, user config, default off. |
| `BAKAR_SCCACHE_DIST` | `sccache_dist` | CLI `--sccache-dist`, env, user config, default off. No workspace tier. |
| `BAKAR_CCACHE` | `ccache` | env, workspace `[build]`, user config; when none sets it, follows the resolved `sccache_dist`. |

### Release workspaces and dispatch

| Variable | Description |
|----------|-------------|
| `BAKAR_REPO_POLICY` | `reuse` or `kas`; any other value is refused by the build and the doctor. `reuse` classifies each pinned repository (at its pin and clean: left alone; missing: cloned; anywhere else: a side checkout under `<workspace>/.bakar/pins/`) so a build never moves a clone of a shared release workspace. It is the default for a meta-avocado build routed through `[workspaces]` and for an explicit `-w` that names a configured release workspace, and a dispatched build always runs under it. `kas` restores kas's own behaviour, which moves every clean clone to the pins of the file being built. |
| `BAKAR_WORKTREE_BUILD_DIR` | Overrides `build.worktree_build_dir` for one invocation. A dispatched build always carries the dispatching host's template (the default when none is configured), so the remote never picks another directory than the plan names. |

### Buildtools

| Variable | Description |
|----------|-------------|
| `BAKAR_BUILDTOOLS_DIR` | Directory holding the buildtools-extended `environment-setup-*` script. Host builds source that script before running bitbake. Resolution order: a toolchain already sourced in the shell (`OECORE_NATIVE_SYSROOT` set and its `usr/bin/gcc` present) wins; then this variable, which beats every config key, including the release-scoped `[build.buildtools_dirs]` entry; then `[build.buildtools_dirs]` for the build's release; then `[build] buildtools_dir` for callers that do not distinguish a release. A non-empty value is final: if the directory has no `environment-setup-*` script, detection reports the toolchain as absent and does not fall back to the config keys. |

### bitbake-override

| Variable | Description |
|----------|-------------|
| `BAKAR_BITBAKE_OVERRIDE` | Set to `0`, `false`, `no` or `off` (any case, surrounding whitespace ignored) to disable the bitbake override step even when a `sources/bitbake` symlink exists. Unset, empty or any other value leaves the step enabled. The status line and doctor check report the spelling you set, for example `BAKAR_BITBAKE_OVERRIDE=off`. |
| `BAKAR_BITBAKE_OVERRIDE_REPO` | Path to a local bitbake repo to swap in. |
| `BAKAR_BITBAKE_OVERRIDE_BRANCH` | Branch to check out in the override repo. |

---

## Built-in BSP defaults

When no config source provides a value, bakar falls back to these per-family
built-in defaults.

| | NXP i.MX | TI Sitara | BYO/generic |
|-|----------|-----------|-------------|
| Machine | `imx8mp-var-dart` | `am62x-var-som` | *(from kas YAML)* |
| Distro | `fsl-imx-xwayland` | `arago` | *(from kas YAML)* |
| Image | `core-image-minimal` | `var-thin-image` | *(from kas YAML)* |
| Manifest | `imx-6.6.52-2.2.2.xml` | *(Processor SDK filename)* | *(kas YAML path)* |
| Repo URL | `https://github.com/varigit/variscite-bsp-platform.git` | *(oe-layertool)* | *(lockfile)* |

---

## See also

- [settings.md](settings.md) - `bakar settings` CLI (read/write config.toml)
- [configuration.md](configuration.md) - resolution order, narrative overview
- [workspace.md](workspace.md) - workspace detection, BSP families, directory layout
- [hashserv.md](hashserv.md) - `build.hashserv` persistent daemon details
- [doctor.md](doctor.md) - `build.show_doctor_report` and `pressure_max_*` pre-flight checks
