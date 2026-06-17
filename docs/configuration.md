# Configuration

bakar resolves configuration from five sources in priority order (highest first):

1. **CLI flags** (e.g. `-m imx8mp-var-dart`)
2. **`BAKAR_*` environment variables**
3. **Workspace `.bakar.toml`** - per-workspace defaults written by `bakar init`
4. **`~/.config/bakar/config.toml`** - persistent per-user defaults
5. **Built-in BSP defaults** - per-family fallbacks compiled into bakar

The resolution order is: `CLI flag > BAKAR_* env var > workspace .bakar.toml > user config.toml > built-in default`.

## Workspace .bakar.toml

The workspace marker file at `<workspace>/.bakar.toml` may carry per-workspace
defaults in `[defaults.<family>]` sections that mirror the `config.toml` schema.
These sit below env vars and above the user `config.toml`, so a workspace value
overrides the per-user default but yields to a one-shot `BAKAR_*` env var or CLI
flag.

| Family | Keys |
|--------|------|
| `[defaults.nxp]` | `manifest`, `machine`, `distro`, `image` |
| `[defaults.ti]` | `manifest`, `machine`, `distro`, `image` |
| `[defaults.generic]` | `kas_yaml`, `machine` |

The same file may also carry a top-level `[host]` table (not under
`[defaults]`) overriding the user `config.toml` `[host]` doctor thresholds for
this workspace: precedence is workspace `.bakar.toml` `[host]` > user
`config.toml` `[host]` > built-in floor. See
[config-reference.md](config-reference.md) for the `[host]` keys.

`bakar init` writes these on workspace creation. See [init.md](init.md) for the
wizard and [workspace.md](workspace.md) for the full `.bakar.toml` schema.

## Environment variables

| Variable | Overrides | Example |
|----------|-----------|---------|
| `BAKAR_MACHINE` | `--machine` | `export BAKAR_MACHINE=imx8mp-var-dart` |
| `BAKAR_DISTRO` | `--distro` | `export BAKAR_DISTRO=fsl-imx-xwayland` |
| `BAKAR_IMAGE` | `--image` | `export BAKAR_IMAGE=var-thin-image` |
| `BAKAR_MANIFEST` | `--manifest` | `export BAKAR_MANIFEST=imx-6.12.49-2.2.0.xml` |
| `BAKAR_REPO_BRANCH` | `--branch` | `export BAKAR_REPO_BRANCH=lf-6.12.y-var01` |
| `BAKAR_REPO_URL` | (repo manifest URL) | `export BAKAR_REPO_URL=https://github.com/Variscite/...` |
| `KAS_CONTAINER_IMAGE` | `build.container_image` | `export KAS_CONTAINER_IMAGE=ghcr.io/siemens/kas/kas:4.7` |
| `BAKAR_BITBAKE_OVERRIDE_REPO` | `--repo` in `bitbake-override` | `export BAKAR_BITBAKE_OVERRIDE_REPO=~/src/bitbake` |

## config.toml

Written to `~/.config/bakar/config.toml`. Managed via `bakar settings` or edited directly.

```toml
[defaults.nxp]
machine = "imx8mp-var-dart"
distro = "fsl-imx-xwayland"
image = "var-thin-image"
manifest = "imx-6.12.49-2.2.0.xml"

[defaults.ti]
machine = "am62x-var-som"
manifest = "processor-sdk-10.1.0.8-config_var1.txt"

[build]
container_image = "ghcr.io/siemens/kas/kas:4.7"  # custom kas-container image
show_doctor_report = true   # show the doctor report (checks always run before every build)
dl_dir = "/mnt/yocto-cache/downloads"
sstate_dir = "/mnt/yocto-cache/sstate"
sstate_mirrors = "file:///mnt/sstate/PATH;downloadfilename=PATH"

# HTTP sstate mirror URL. When set, bakar appends the shared-cache overlay
# (bakar-tuning-shared-cache.yml) to every build, wiring SSTATE_MIRRORS to the
# mirror using the /all/PATH;downloadfilename=PATH layout and BB_HASHSERVE_UPSTREAM
# for hash equivalence. The official Yocto Project mirror works directly:
#   sstate_mirror_url = "http://sstate.yoctoproject.org"
# Equivalent to passing --sstate-mirror on every bakar build invocation.
# sstate_mirror_url = "https://cache.example.com"
scheduler = "completion"  # or "speed"

# PSI pressure gate (written by psi_autocalibrate below)
pressure_max_cpu = 72
pressure_max_io = 41
pressure_max_memory = 20

# Auto-calibrate the PSI thresholds: when true, `bakar build` samples
# /proc/pressure during the build and writes the recommended pressure_max_*
# back here afterwards, reporting what changed. The first build bootstraps
# the values; later builds only raise a threshold (from an unthrottled
# measurement), never lower it - a light sstate-cached build must not
# over-throttle the next cold one. To recalibrate from scratch (e.g. after
# a hardware change), delete the pressure_max_* lines above.
psi_autocalibrate = true

# Stall self-guard. When every running task's log has been silent this many
# seconds (a wedged task, e.g. a deadlocked final link), bakar SIGINTs the build
# so it fails cleanly with a stall-timeout step naming the stuck task instead of
# spinning until you Ctrl-C. The signal is per-task log freshness, not raw output
# (bitbake's own keepalive lines would otherwise mask a hang). 0 disables.
stall_abort_secs = 2700

# Persistent hash equivalence daemon (off by default; see hashserv.md).
# When true, bakar spawns and reuses a workspace-scoped bitbake-hashserv
# so OEEquivHash sstate equivalence accumulates across builds.
hashserv = true

# ccache location. Per-workspace (<workspace>/ccache) by default. Set
# ccache_shared to reuse one cache across every workspace (cross-BSP hits,
# less disk), defaulting to ~/.cache/bakar/ccache. ccache_dir pins an explicit
# shared path and takes precedence over ccache_shared.
#
# NOTE: with ccache_shared = true the SAME size cap governs every workspace's
# builds. bakar caps the build ccache at 50G (CCACHE_MAXSIZE in the tuning
# overlays); a single shared cache feeding many BSPs may evict under that cap.
# Raise it by exporting a larger CCACHE_MAXSIZE in your shell before building
# (it overrides the overlay value) when you share across several workspaces.
ccache_shared = true
# ccache_dir = "/mnt/yocto-cache/ccache"

# Doctor host-environment thresholds. Defaults equal the values doctor
# previously hardcoded, so an absent [host] table is a no-op. A workspace
# .bakar.toml [host] table overrides these; both override the built-in floor.
[host]
inotify_instances = 4096   # fs.inotify.max_user_instances floor
inotify_watches = 524288   # fs.inotify.max_user_watches floor
swappiness_max = 20        # vm.swappiness ceiling
nofile_soft = 8192         # docker default-ulimits nofile soft floor
mem_min_gb = 16.0          # minimum available+swap memory floor (GB)

[layers]
show_hashes = true   # always print layer SHAs after build/sync
```

## vendors.toml

For custom board families not covered by the built-in NXP/TI/bbsetup presets. Written to `~/.config/bakar/vendors.toml`.

```toml
[[vendors]]
name           = "my-board"
family         = "nxp"               # base family: nxp, ti, generic, or bbsetup
manifest_regex = "my-board-.*\\.xml" # Python regex matched against manifest filename

# Override any BspModel field:
default_machine = "my-imx8mp-board"
default_distro  = "my-custom-distro"
default_image   = "my-image"
```

Vendor entries are checked before built-in regexes. `bakar build -f my-board-2.2.0.xml` dispatches to your vendor config automatically. See [config-reference.md](config-reference.md) for the full vendors.toml field reference.

## Build telemetry directories

Every build run writes to:

```text
<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/
  events.jsonl          - structured step events (machine-readable)
  console.log           - human-readable step progress
  kas.log               - stdout+stderr from kas-container
  time.log              - wall-clock timing per step
  du.tsv                - disk usage snapshot after build
  env.txt               - environment variables at build time
  diagnosis.txt         - doctor check results
  bitbake_eventlog.json - raw bitbake event log (JSON Lines, base64-pickle payloads)
  bitbake-events.json   - normalized event artifact parsed from the raw log
```

`bitbake_eventlog.json` is written by bitbake itself: `bakar` injects
`BB_DEFAULT_EVENTLOG` (pointing at this path) into the build environment, so
`bakar build`, `bakar bitbake`, and `bakar clean-recipe` all leave one. It is
JSON Lines where each event payload is a base64-encoded Python pickle.

`bitbake-events.json` is the normalized artifact `bakar` parses from the raw
log on the host (without importing bitbake). It is the stable contract triage
and downstream tooling consume. Top-level shape:

```json
{
  "schema_version": 1,
  "build": {"started": "<iso>", "completed": "<iso|null>", "outcome": "success|failed|unknown",
            "preset": "<name|null>", "release": "<id|null>", "run_id": "<ts>"},
  "tasks": [{"recipe": "<PF>", "task": "do_compile", "outcome": "succeeded|failed|failed_silent",
             "started": "<iso|null>", "completed": "<iso|null>", "pid": 1234, "logfile": "<path|null>"}],
  "setscene": {"covered": 0, "notcovered": 0, "total": 0,
               "per_recipe": [{"recipe": "<PF>", "covered": 0, "notcovered": 0}]},
  "failures": [{"recipe": "<PF>", "task": "do_compile", "logfile": "<path>", "errprinted": true}]
}
```

`bakar triage` reads `failures[]` from this artifact to name the failing
recipe/task and print the recorded logfile excerpt. See [triage.md](triage.md).

## See also

- [config-reference.md](config-reference.md) - complete option reference (all fields, types, defaults)
- [settings.md](settings.md) - CRUD interface for config.toml
- [workspace.md](workspace.md) - workspace detection and BSP families
- [hashserv.md](hashserv.md) - `[build] hashserv` persistent daemon details
