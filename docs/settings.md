# bakar settings

Read and write recognized settings in `~/.config/bakar/config.toml`.

## Synopsis

```text
bakar settings list
bakar settings get KEY
bakar settings set KEY VALUE
bakar settings unset KEY
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | Print all recognized keys with current values |
| `get KEY` | Print the current value of one key |
| `set KEY VALUE` | Validate, coerce, and write a key |
| `unset KEY` | Remove a key from the config file |

## Key reference

All keys use dotted notation (`section.subsection.key`).

### NXP defaults (`defaults.nxp.*`)

| Key | Type | Description |
|-----|------|-------------|
| `defaults.nxp.machine` | string | Default machine (e.g. `imx8mp-var-dart`) |
| `defaults.nxp.distro` | string | Default distro (e.g. `fsl-imx-xwayland`) |
| `defaults.nxp.image` | string | Default image (e.g. `var-thin-image`) |
| `defaults.nxp.manifest` | string | Default manifest filename |
| `defaults.nxp.repo_url` | string | Override repo manifest URL |

### TI defaults (`defaults.ti.*`)

| Key | Type | Description |
|-----|------|-------------|
| `defaults.ti.machine` | string | Default machine (e.g. `am62x-var-som`) |
| `defaults.ti.distro` | string | Default distro |
| `defaults.ti.image` | string | Default image |
| `defaults.ti.manifest` | string | Default manifest filename |

### Build settings (`build.*`)

| Key | Type | Description |
|-----|------|-------------|
| `build.kas_container_image` | string | Custom kas-container image tag |
| `build.show_doctor_report` | bool | Show the doctor report before every build (default: `true`); doctor checks always run regardless |
| `build.dl_dir` | string | Override `DL_DIR` (shared download cache) |
| `build.sstate_dir` | string | Override `SSTATE_DIR` (sstate cache) |
| `build.sstate_mirrors` | string | `SSTATE_MIRRORS` value for remote cache |
| `build.scheduler` | string | BitBake scheduler (`speed`, `completion`) |
| `build.pressure_max_cpu` | int | PSI cpu avg10 threshold to throttle bitbake task scheduling |
| `build.pressure_max_io` | int | PSI io avg10 threshold |
| `build.pressure_max_memory` | int | PSI memory avg10 threshold |
| `build.psi_autocalibrate` | bool | Auto-write `pressure_max_*` after each build from sampled PSI peaks (default: `false`) |
| `build.stall_abort_secs` | int | Abort the build when every running task's log is silent this many seconds, naming the wedged task (default: `2700`; `0` disables) |
| `build.hashserv` | bool | Persistent workspace-scoped bitbake-hashserv daemon (default: `false`). See [hashserv.md](hashserv.md). |
| `build.ccache_shared` | bool | Share one ccache across all workspaces instead of per-workspace (default: `false`). Defaults the cache to `~/.cache/bakar/ccache`. |
| `build.ccache_dir` | string | Explicit ccache directory (a shared location of your choosing); overrides `ccache_shared` and the per-workspace default. |
| `build.ccache` | bool | Enable ccache (default: `true`). Mutually exclusive with `sccache_dist`. |
| `build.sccache_dist` | bool | Route C/C++ `do_compile` through sccache-dist (default: `false`). See [sccache-dist.md](sccache-dist.md). |
| `build.sccache_scheduler_url` | string | sccache-dist scheduler URL (e.g. `http://localhost:10600`) |
| `build.rm_work` | bool | Strip each recipe's WORKDIR after it builds (default: `false`; off while bakar is in use) |
| `build.disk_free_threshold_gb` | float | Minimum free disk (GB) enforced by the doctor `disk-free` check (default: `50.0`) |
| `build.nproc` | int | Base build parallelism, exported as `NPROC` (default: auto-detected `os.cpu_count()`) |
| `build.parallel_make` | int | Compile `-j` (`PARALLEL_MAKE`), decoupled from recipe concurrency (default: falls back to `nproc`) |
| `build.bb_number_threads` | int | Recipe concurrency (`BB_NUMBER_THREADS`), decoupled from compile `-j` (default: falls back to `nproc`) |

### Host thresholds (`host.*`)

Floors and ceilings the `bakar doctor` host-environment checks compare live
system state against. Defaults equal the values doctor previously hardcoded, so
the verdicts are unchanged until you set a key. A workspace `.bakar.toml`
`[host]` table overrides the user `config.toml` `[host]` section, which overrides
the built-in floor.

| Key | Type | Description |
|-----|------|-------------|
| `host.inotify_instances` | int | `fs.inotify.max_user_instances` sysctl floor (default: `4096`) |
| `host.inotify_watches` | int | `fs.inotify.max_user_watches` sysctl floor (default: `524288`) |
| `host.swappiness_max` | int | `vm.swappiness` sysctl ceiling (default: `20`) |
| `host.nofile_soft` | int | Docker `default-ulimits` `nofile` soft floor (default: `8192`) |
| `host.mem_min_gb` | float | Minimum available+swap memory floor in GB (default: `16.0`) |

### Layers settings (`layers.*`)

| Key | Type | Description |
|-----|------|-------------|
| `layers.show_hashes` | bool | Always print layer hashes after build/sync |
| `layers.show_sstate_summary` | bool | Always show the sstate cache summary in `bakar report` |

## Examples

```bash
# View all settings
bakar settings list

# Set a default machine so you don't need -m on every invocation
bakar settings set defaults.nxp.machine imx8mp-var-dart
bakar settings set defaults.nxp.manifest imx-6.12.49-2.2.0.xml

# Point builds at a shared download cache
bakar settings set build.dl_dir /mnt/yocto-cache/downloads
bakar settings set build.sstate_dir /mnt/yocto-cache/sstate

# Use a sstate mirror
bakar settings set build.sstate_mirrors "file:///mnt/sstate/PATH;downloadfilename=PATH"

# Hide the doctor report (checks still run; only build-blocking issues print)
bakar settings set build.show_doctor_report false

# Always show layer hashes after sync/build
bakar settings set layers.show_hashes true

# Raise a doctor host threshold above its built-in floor
bakar settings set host.inotify_instances 8192
bakar settings set host.mem_min_gb 32.0

# Check the current value of a key
bakar settings get defaults.nxp.machine

# Remove a setting (reverts to built-in default)
bakar settings unset defaults.nxp.machine
```

## Notes

- Boolean keys accept `true`/`false`/`1`/`0`.
- Unknown keys are rejected immediately (before touching the file).
- Writes are atomic; a crash mid-write leaves the existing config intact.
- These settings are the lowest-priority layer in the resolution chain: CLI flags and `BAKAR_*` env vars override them. See [configuration.md](configuration.md).

## See also

- [configuration.md](configuration.md) - full config resolution chain and env vars
