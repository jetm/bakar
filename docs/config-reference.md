# Configuration Reference

Complete reference for all bakar configuration options across the three config
files. For the resolution order and an introduction, see
[configuration.md](configuration.md). To read and write `config.toml` via the
CLI, see [settings.md](settings.md).

## Resolution order (highest priority first)

1. CLI flag (`-m`, `-f`, `--sstate-mirror`, ...)
2. `BAKAR_*` environment variable
3. Workspace `.bakar.toml` (`[defaults.<family>]`)
4. User `~/.config/bakar/config.toml`
5. Built-in BSP default

---

## `~/.config/bakar/config.toml`

Managed with `bakar settings list/get/set/unset`. Editable directly as TOML.
Writes are atomic; a crash mid-write leaves the previous file intact.
Unknown keys are rejected by `bakar settings set` but silently ignored when the
file is loaded directly (future keys added by a newer bakar won't break an older
one).

### `[defaults.nxp]` — NXP i.MX default targets

| Key | Type | Built-in default | Description |
|-----|------|-----------------|-------------|
| `machine` | string | `imx8mp-var-dart` | Default `MACHINE` for NXP builds. Overridden by `--machine`/`-m` or `BAKAR_MACHINE`. |
| `distro` | string | `fsl-imx-xwayland` | Default `DISTRO`. Overridden by `--distro`/`-d` or `BAKAR_DISTRO`. |
| `image` | string | `core-image-minimal` | Default bitbake image target. Overridden by `--image`/`-i` or `BAKAR_IMAGE`. |
| `manifest` | string | `imx-6.6.52-2.2.2.xml` | Default repo manifest filename. Overridden by `--manifest`/`-f` or `BAKAR_MANIFEST`. |
| `repo_url` | string | `https://github.com/varigit/variscite-bsp-platform.git` | Override the variscite-bsp-platform manifest repo URL. Overridden by `BAKAR_REPO_URL`. |

### `[defaults.ti]` — TI Sitara default targets

| Key | Type | Built-in default | Description |
|-----|------|-----------------|-------------|
| `machine` | string | `am62x-var-som` | Default `MACHINE` for TI builds. Overridden by `--machine`/`-m` or `BAKAR_MACHINE`. |
| `distro` | string | `arago` | Default `DISTRO`. Overridden by `--distro`/`-d` or `BAKAR_DISTRO`. |
| `image` | string | `var-thin-image` | Default image target. Overridden by `--image`/`-i` or `BAKAR_IMAGE`. |
| `manifest` | string | *(long Processor SDK filename)* | Default oe-layertool config filename. Overridden by `--manifest`/`-f` or `BAKAR_MANIFEST`. |

### `[build]` — build pipeline behaviour

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `show_doctor_report` | bool | `true` | Show the pre-flight doctor report before every build and sync. Set to `false` to print only build-blocking issues; the global `--hide-doctor-report` flag does the same for one invocation. Doctor checks always run - a BLOCK-severity failure aborts regardless. |
| `kas_container_image` | string | `jetm/kas-build-env:latest` | kas-container image tag. Overridden by a workspace `.bakar.toml` `[build]` value and by the `KAS_CONTAINER_IMAGE` env var. |
| `dl_dir` | string | *(not set)* | Override `DL_DIR` (shared download cache path). Passed to kas-container as an env var. |
| `sstate_dir` | string | *(not set)* | Override `SSTATE_DIR` (sstate cache path). Passed to kas-container as an env var. |
| `sstate_mirrors` | string | *(not set)* | Raw `SSTATE_MIRRORS` value passed to the build. Use `sstate_mirror_url` unless you need full control over the mirror syntax. |
| `sstate_mirror_url` | string | *(not set)* | HTTP(S) sstate/downloads mirror URL. When set, bakar activates the shared-cache overlay (`bakar-tuning-shared-cache.yml`), which wires `SSTATE_MIRRORS` to `<url>/all/PATH;downloadfilename=PATH` and sets `BB_HASHSERVE_UPSTREAM`. Equivalent to passing `--sstate-mirror` on every invocation. The Yocto Project public mirror (`http://sstate.yoctoproject.org`) works without any additional config. |
| `scheduler` | string | *(not set)* | BitBake task scheduler: `speed` (maximize parallelism) or `completion` (minimize task switches). |
| `pressure_max_cpu` | float | *(not set)* | PSI cpu avg10 pressure threshold (0–100). Exported to the build as `BB_PRESSURE_MAX_CPU` converted to bitbake's stall-microseconds-per-second scale (percent × 10,000); bitbake stops launching new tasks while the measured stall rate exceeds it. Must be `>= 1`. Generate calibrated values with `psi_autocalibrate`. |
| `pressure_max_io` | float | *(not set)* | PSI io avg10 threshold. Same semantics as `pressure_max_cpu`. |
| `pressure_max_memory` | float | *(not set)* | PSI memory avg10 threshold. Same semantics as `pressure_max_cpu`. |
| `psi_autocalibrate` | bool | `false` | When `true`, bakar samples `/proc/pressure` during each build and writes updated `pressure_max_*` values back to this file after completion. The first build bootstraps the values; subsequent builds only raise a threshold (from an unthrottled measurement), never lower it, so a light sstate-cached build cannot over-throttle the next cold one. Delete the `pressure_max_*` keys to recalibrate from scratch. |
| `disk_free_threshold_gb` | float | `50.0` | Minimum free disk space (GB) enforced by the doctor `disk-free` check before each build. Must be `> 0`. |
| `stall_abort_secs` | int | `2700` | Self-guard against a wedged task (e.g. a deadlocked final link). When every running task's log has been silent this many seconds, bakar SIGINTs the build so it fails cleanly with a `stall-timeout` step naming the stuck task, instead of spinning until you `Ctrl-C`. The signal is per-task log freshness, not raw output (bitbake's own keepalive lines would otherwise mask a hang). Set to `0` to disable. Must be `>= 0`. |
| `stop_on_error` | bool | `true` | SIGINT the build the moment any task fails, rather than waiting for bitbake's own halt-on-failure to drain every already-running task on its own schedule (which can take a long time - an unrelated `do_compile` can still be running when the failure landed). bitbake already stops scheduling *new* tasks on first failure regardless of this setting; this only stops bakar from rendering a misleadingly-normal live view while it waits. Set to `false` to fall back to waiting for the natural drain. |
| `hashserv` | bool | `false` | Persistent workspace-scoped `bitbake-hashserv` daemon. When `true`, bakar starts a hashserv instance on the first build and reuses it on subsequent ones, accumulating `OEEquivHash` sstate equivalence across builds. See [hashserv.md](hashserv.md). |
| `ccache_shared` | bool | `false` | Share one ccache across all workspaces instead of a per-workspace cache. Default shared path: `~/.cache/bakar/ccache`. Cross-BSP compiler cache hits become possible. Note: a shared cache is subject to its own size cap (`CCACHE_MAXSIZE` from the tuning overlay, default 50 GB); raise it with `export CCACHE_MAXSIZE=100G` when sharing across many workspaces. |
| `ccache_dir` | string | *(not set)* | Explicit ccache directory. Takes precedence over `ccache_shared` and the per-workspace default. Useful for pointing several machines at a shared NFS-mounted cache. |
| `ccache` | bool | `true` | Enable ccache. Mutually exclusive with `sccache_dist` - chaining the two launchers double-wraps `CC`, so enabling `sccache_dist` removes ccache for that build. |
| `sccache_dist` | bool | `false` | Route C/C++ `do_compile` through an sccache-dist cluster. Requires `sccache_scheduler_url` (or the global `--sccache-scheduler`). See [sccache-dist.md](sccache-dist.md). |
| `sccache_scheduler_url` | string | *(not set)* | sccache-dist scheduler URL, e.g. `http://localhost:10600`. Overridden by the global `--sccache-scheduler` flag. |
| `rm_work` | bool | `false` | Strip each recipe's WORKDIR after it builds. Off by default: while bakar is in use the tuning stack keeps work dirs so `inspect`/`diffsigs`/devshell still work. |
| `nproc` | int | *(auto: `os.cpu_count()`)* | Base build parallelism, exported as the `NPROC` env var. The tuning overlays fan it out to `BB_NUMBER_THREADS`, `PARALLEL_MAKE`, and `BB_NUMBER_PARSE_THREADS` unless `parallel_make` / `bb_number_threads` override them. A live `NPROC=` env var still wins over this value. Must be `> 0`. |
| `parallel_make` | int | *(falls back to `nproc`)* | Compile `-j` (`PARALLEL_MAKE`), decoupled from recipe concurrency. Size it to the total cores available to compilation - for an sccache-dist cluster that is the sum across all build servers, so compile jobs spill to remote nodes. Exported as `BAKAR_PARALLEL_MAKE`. Must be `> 0`. |
| `bb_number_threads` | int | *(falls back to `nproc`)* | Recipe concurrency (`BB_NUMBER_THREADS`; `BB_NUMBER_PARSE_THREADS` follows it), decoupled from compile `-j`. Size it to local RAM - too many parallel recipes OOM a full image build. Exported as `BAKAR_BB_NUMBER_THREADS`. Must be `> 0`. |

### `[host]` — doctor host-environment thresholds

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

### `[layers]` — display preferences

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
An unrecognized key under a known `[defaults.<family>]` section emits a warning
but does not fail the load. Unknown sections are silently ignored.

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
| `kas_container_image` | string | Workspace override for the kas-container image tag. Setting it also disables host-mode auto-enable (the workspace has a container setup). |

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

Edited by hand — there is no `bakar settings` integration for this file.
An absent file produces an empty vendor list.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Identifier shown in logs and error messages. |
| `family` | string | yes | Base BSP family to inherit from: `nxp`, `ti`, `generic`, or `bbsetup`. |
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
| `KAS_CONTAINER_IMAGE` | Override the kas-container image for one invocation. Takes precedence over both the workspace `.bakar.toml` `[build]` value and `build.kas_container_image` in the user config. |

### bitbake-override

| Variable | Description |
|----------|-------------|
| `BAKAR_BITBAKE_OVERRIDE` | Set to `0` to disable the bitbake override step even when a `sources/bitbake` symlink exists. |
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

- [settings.md](settings.md) — `bakar settings` CLI (read/write config.toml)
- [configuration.md](configuration.md) — resolution order, narrative overview
- [workspace.md](workspace.md) — workspace detection, BSP families, directory layout
- [hashserv.md](hashserv.md) — `build.hashserv` persistent daemon details
- [doctor.md](doctor.md) — `build.show_doctor_report` and `pressure_max_*` pre-flight checks
