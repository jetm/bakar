# bspctl

bspctl is a kas wrapper for Yocto BSP builds. kas is the modern Yocto build
tool that describes a stack as a YAML topology (repos, layers, machine,
distro) and drives `bitbake` on the host; `kas-container` wraps it in a
Docker container for reproducibility. bspctl defaults to `kas-container` and
adds the rest of the workflow that kas leaves to you:

1. Layers a curated tuning overlay on top of your kas YAML at build time
   (ccache, MIRRORS, PREMIRRORS, fetch robustness, BSP-specific knobs)
   without touching the YAML on disk
2. Runs pre-flight checks (`bspctl doctor`) before kicking off `kas-container
   build` so a wrong container Python, full disk, or broken cache fails fast
   instead of four hours in
3. Captures structured per-run telemetry under `build/runs/<ts>/` (event log,
   kas output, env snapshot, timing, disk usage)
4. Ships `bspctl triage` to read the structured logs, locate the failing
   recipe log, and match against a suggestion table

Works with any kas YAML in generic mode - bring your own.

For vendor BSPs that ship as Google repo tool XML manifests (NXP i.MX),
oe-layertool configs (TI Sitara), or bitbake-setup workspaces (Yocto 5.3+)
instead of kas YAML, bspctl bridges the gap:

- `bspctl sync` populates `sources/` by running the right tool for the
  manifest, and skips when sources are already current
- `bspctl gen-kas` translates the vendor manifest into a kas YAML
  (topology only: machine + repos + layers), so the rest of the pipeline
  above applies unchanged
- For bitbake-setup workspaces, `bspctl build` translates
  `config/config-upstream.json` (and the optional pinned-SHA file) into a
  deterministic kas YAML on each run - no manifest and no sync step needed
  because bitbake-setup already fetched the layers

| Without bspctl | With bspctl |
|----------------|-------------|
| Add ccache, mirror, fetch, and EULA boilerplate to every project YAML | Curated overlay applied at build time; your YAML stays topology-only |
| Start a 4-hour build only to fail on a full disk or wrong container Python | `bspctl doctor` catches environment problems before kas starts |
| Grep through `build/tmp/work/.../temp/log.do_*` to find what failed | `bspctl triage` reads structured run logs, locates the recipe log, and matches against a suggestion table |
| No build history | Per-run directory: structured event log, kas output, env snapshot, disk usage, timing |
| repo tool XML and kas YAML describe the same thing but don't interoperate | `bspctl gen-kas` translates the vendor XML manifest into a kas YAML |
| Re-run repo tool or oe-layertool by hand on every fresh workspace | `bspctl sync` fetches sources and skips if already current |
| bitbake-setup workspace needs a hand-authored kas YAML to build with kas | `bspctl build` translates `config-upstream.json` automatically on each run |

The NXP/TI presets are the batteries-included path, not a requirement.

## Installation

```bash
uv tool install bspctl
```

```bash
pip install bspctl
```

To install from source (development):

```bash
git clone https://github.com/jetm/bspctl
cd bspctl
uv tool install --editable .
```

Requires Python 3.11+. The bitbake parser compatibility constraint (3.13+
deadlocks the parser fork) applies to the container image, not the host
tool - bspctl runs fine on any supported Python including 3.14.

## Quickstart

```bash
cd ~/bsp-workspace

# NXP i.MX - manifest drives everything
bspctl build -f imx-6.12.49-2.2.0.xml -m imx95-var-dart

# TI Sitara
bspctl build -f processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt \
             -m am62x-var-som

# bitbake-setup workspace (Yocto 5.3+) - auto-detected from CWD
cd ~/bbsetup-workspace
bspctl build

# BYO YAML (generic or NXP/TI)
bspctl build path/to/my-build.yml
```

On a clean workspace, the first run populates `sources/` via repo sync and
takes hours. Subsequent runs skip sync automatically and go straight to
bitbake.

### Example run

```text
❯ bspctl build examples/kas-qemux86-64-wrynose.yml
:: bspctl build  BYO examples/kas-qemux86-64-wrynose.yml
INFO     build mode=byo bsp=generic yaml=examples/kas-qemux86-64-wrynose.yml
         overlay=bspctl/overlays/bspctl-tuning-generic.yml
INFO     → doctor
                                                         Pre-flight diagnosis
 Check             ┃ Sev   ┃ Status ┃ Detail
━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 host-tools        │ BLOCK │ PASS   │ GENERIC required binaries present (kas-container, docker, python3)
 docker-daemon     │ BLOCK │ PASS   │ server 29.5.1
 container-image   │ BLOCK │ PASS   │ jetm/kas-build-env:latest present
 container-os      │ BLOCK │ PASS   │ fedora  / Python 3.12.10
 container-bitbake │ INFO  │ SKIP   │ inspection failed
 cache-dirs        │ BLOCK │ PASS   │ SSTATE_DIR=/mnt/sstate, DL_DIR=/mnt/downloads
 sysctl            │ WARN  │ PASS   │ inotify/swappiness sane
 docker-ulimits    │ WARN  │ PASS   │ nofile soft=65536
 disk-free         │ BLOCK │ PASS   │ >= 50G free on each mount
 memory            │ WARN  │ PASS   │ available+swap=173717M
 bitbake-override  │ INFO  │ SKIP   │ poky tree absent (pre-bootstrap)
 bitbake-locks     │ BLOCK │ PASS   │ no stale locks or sockets
INFO     ✓ doctor
INFO     ↷ bitbake_override (generic mode)
INFO     → kas_build
INFO     exec: kas-container --runtime-args -v examples/ccache:/work/ccache:rw build \
         kas-qemux86-64-wrynose.yml:.bspctl/overlays/bspctl-tuning-generic.yml
kas_build ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╸ 5190/5191 tasks core-image-minimal.bb:do_create_image_sbom_spdx live 1s  +453M 0:23:10
INFO     ✓ kas_build
build succeeded
artifacts: examples/build/tmp/deploy/images/generic
```

## What bspctl adds

### Source sync

kas has no concept of fetching BSP sources. NXP i.MX ships a repo tool
manifest; TI Sitara ships an oe-layertool config. bspctl runs the right
tool and caches the result - re-running `bspctl build` on a populated
workspace skips sync entirely.

```bash
bspctl sync --manifest imx-6.12.49-2.2.0.xml   # explicit sync step
bspctl build -f imx-6.12.49-2.2.0.xml          # sync + build in one shot
```

bitbake-setup workspaces skip this step entirely - `bitbake-setup init`
already populated `layers/` before bspctl enters the picture.

### bitbake-setup workspaces

[bitbake-setup](https://docs.yoctoproject.org/dev/ref-manual/devtool-reference.html)
is a Yocto 5.3+ workspace tool that writes a resolved registry config to
`config/config-upstream.json`. bspctl reads that file and the optional
pinned-SHA file (`config/sources-fixed-revisions.json`) and translates them
into a kas YAML on each build run.

Prerequisites before running `bspctl build` from a bitbake-setup workspace:

1. `bitbake-setup init` - fetches `layers/` and writes `build/init-build-env`
2. The workspace must include a `machine/<name>` fragment in its config, or
   you must pass `--machine` on the command line

```bash
# Initialize the workspace once with bitbake-setup
bitbake-setup init

# Then build from inside the workspace (or any subdirectory)
cd ~/bbsetup-workspace
bspctl build                     # machine from config fragment
bspctl build --machine qemuarm64 # explicit machine override
bspctl doctor                    # pre-flight checks only
```

bspctl regenerates `kas-bbsetup.yml` on every `bspctl build` run from the
live config files. The file is a build artifact - add it to `.gitignore`
rather than committing it. When `sources-fixed-revisions.json` is present,
the translated YAML pins each repo to its recorded SHA with no `branch:` key,
which avoids kas reachability check failures when the upstream branch has
moved forward.

### Tuning overlay

Every build - BYO or manifest-driven - layers a static tuning file onto
your kas YAML at run time. Your YAML is not modified on disk; kas merges
the overlay in when it parses the build.

What the overlay unions in:

- `CCACHE_DIR` + `INHERIT += "ccache"`
- `BB_NUMBER_THREADS` / `PARALLEL_MAKE` from `$NPROC`
- `IMAGE_FEATURES:remove` (strips dev/dbg packages from images)
- `BB_FETCH_TIMEOUT = "600"` (raise the per-URI timeout)
- `MIRRORS` (replace scarthgap's dead `sources.openembedded.org` with
  the Yocto Project mirror)
- `PREMIRRORS:prepend` for github.com (silent fallback, not a build blocker)
- `FETCHCMD_wget` (crates.io 403 workaround for Rust recipes)
- `PYTHONMALLOC=malloc` (reduces bitbake parser fork-race rate on CPython 3.13)
- **NXP only**: `ACCEPT_FSL_EULA`, renderdoc CMake-launcher fix,
  linux-imx fork PREMIRROR, `meta-varis-overrides` layer
- **TI only**: ti-linux-kernel + ti-u-boot fork PREMIRRORs,
  `meta-varis-overrides-ti` layer

To override a knob, pass a second kas YAML as a colon-joined overlay:

```bash
bspctl build my-build.yml:my-tuning.yml
```

`my-tuning.yml` is merged on top of the bspctl default overlay at build time.
The built-in overlays ship inside the package and are not user-editable directly.

### Pre-flight checks

`bspctl doctor` runs before every build (and standalone). BLOCK failures
halt before kas starts; WARN prints and continues.

- Docker daemon reachable and ulimits set
- Container image present locally
- Container Python version (3.13+ deadlocks the bitbake parser fork)
- Disk space on workspace, `SSTATE_DIR`, `DL_DIR`
- NXP: fork repos populated, bitbake override applied
- TI: oe-layertool cloned, active config matches manifest
- bitbake-setup: `config-upstream.json` present and `build/init-build-env` exists

```bash
bspctl doctor               # standalone check, no build
bspctl build --skip-doctor  # bypass (not recommended)
```

### Triage

`bspctl triage` turns a failed build from a grep exercise into a
one-command diagnosis:

1. Reads `events.jsonl` to find the first `step_fail`
2. Tails `kas.log` around the failure
3. Extracts the bitbake "Logfile of failure stored in:" path, rewrites
   the container path to the host path, and tails that recipe log
4. Matches the combined output against a suggestion table: fetch
   failures, parser deadlocks, OOM, disk full, GitHub flakes, missing
   EULA, stale bitbake cache, kas/container version skew

```bash
bspctl triage                      # most recent run
bspctl triage 20260423-091014      # specific run by timestamp
bspctl triage -k path/to/kas.yml  # most recent run for a BYO YAML
```

### Structured run logs

Each `bspctl build` invocation creates `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/`:

| File | Contents |
|------|----------|
| `events.jsonl` | One JSON object per step start/end/error |
| `console.log` | Same stream, human-readable |
| `kas.log` | `kas-container build` stdout + stderr |
| `env.txt` | `BSPCTL_*`, `KAS_*`, `BB_*`, `SSTATE_*`, `DL_*`, `NPROC` at run start |
| `time.log` | `/usr/bin/time -v` output |
| `du.tsv` | `<unix-ts>\t<bytes>` samples of `build/tmp/` every 30 s |
| `diagnosis.txt` | Pre-flight diagnosis as plain text |

## Build forms

```bash
# Manifest-driven, one shot (NXP/TI only)
bspctl build -f imx-6.12.49-2.2.0.xml -m imx95-var-dart

# bitbake-setup workspace - auto-detected from CWD or any subdirectory
cd ~/bbsetup-workspace && bspctl build
bspctl build --machine qemuarm64   # explicit machine (overrides config fragment)

# BYO YAML - skips sync and gen-kas
bspctl build nxp/my-build.yml

# Staged - review the generated YAML before building
bspctl sync --manifest imx-6.12.49-2.2.0.xml
bspctl gen-kas --manifest imx-6.12.49-2.2.0.xml -o nxp/my-build.yml
bspctl build nxp/my-build.yml

# Generic - any kas YAML, bsp_root is the YAML's parent
bspctl build path/to/kas.yml
```

## BSP presets

| Family | Detected by | Source sync | kas YAML | Overlay |
|--------|-------------|-------------|----------|---------|
| **NXP i.MX** | `imx-A.B.C-X.Y.Z.xml` manifest | repo tool | `bspctl gen-kas` | `bspctl-tuning-nxp.yml` |
| **TI Sitara** | `processor-sdk-*-config_var<N>.txt` manifest | oe-layertool | `bspctl gen-kas` | `bspctl-tuning-ti.yml` |
| **bitbake-setup** | `config/config-upstream.json` + `build/init-build-env` | none (layers/ pre-populated) | translated from `config-upstream.json` | `bspctl-tuning-generic.yml` |
| **Generic** | any kas YAML passed as argument | none | user-supplied | `bspctl-tuning-generic.yml` |

BSP detection runs on the manifest filename (manifest-driven), the presence of
`config/config-upstream.json` and `build/init-build-env` (bitbake-setup), or
the YAML's `machine:` value and `repos:` block (BYO). No NXP/TI markers falls
through to generic mode.

## Configuration

CLI flags, `BSPCTL_*` env vars, the user config file, and built-in defaults
form the resolution chain, highest precedence first:

```text
CLI flag > BSPCTL_* env var > config.toml > built-in default
```

| Env var | Flag | Default |
|---------|------|---------|
| `BSPCTL_MACHINE` | `--machine` | `imx8mp-var-dart` |
| `BSPCTL_DISTRO` | `--distro` | `fsl-imx-xwayland` |
| `BSPCTL_IMAGE` | `--image` | `core-image-minimal` |
| `BSPCTL_MANIFEST` | `--manifest` | `imx-6.6.52-2.2.2.xml` |
| `BSPCTL_REPO_URL` | - | `https://github.com/varigit/variscite-bsp-platform.git` |
| `BSPCTL_REPO_BRANCH` | - | inferred from manifest prefix |
| `KAS_CONTAINER_IMAGE` | - | When absent: host mode active (plain `kas`, no Docker). Set to enable container builds. |
| `SSTATE_DIR` | - | kas default (unset = skip check) |
| `DL_DIR` | - | kas default (unset = skip check) |

### Config file

A user-global config file at `~/.config/bspctl/config.toml` sets persistent
defaults that slot between `BSPCTL_*` env vars and the built-in defaults. The
file is optional: when it is absent, bspctl falls back to the built-in defaults
with no error. bspctl never auto-creates it. Missing keys and unknown keys are
ignored; only the keys below are recognized.

```toml
[defaults.nxp]
machine  = "imx8mp-var-dart"
distro   = "fsl-imx-xwayland"
image    = "core-image-minimal"
manifest = "imx-6.6.52-2.2.2.xml"
repo_url = "https://github.com/varigit/variscite-bsp-platform.git"

[defaults.ti]
machine  = "am62x-var-som"
distro   = "arago"
image    = "var-thin-image"
manifest = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt"

[build]
container_image = "jetm/kas-build-env:latest"
doctor          = true

[layers]
show_hashes = false
```

| Section | Key | Default | Effect |
|---------|-----|---------|--------|
| `[defaults.nxp]` | `machine` | `imx8mp-var-dart` | Default NXP machine. |
| `[defaults.nxp]` | `distro` | `fsl-imx-xwayland` | Default NXP distro. |
| `[defaults.nxp]` | `image` | `core-image-minimal` | Default NXP image. |
| `[defaults.nxp]` | `manifest` | `imx-6.6.52-2.2.2.xml` | Default NXP manifest. |
| `[defaults.nxp]` | `repo_url` | varigit BSP platform | repo-tool source URL for NXP. |
| `[defaults.ti]` | `machine` | (none) | Default TI machine. |
| `[defaults.ti]` | `distro` | (none) | Default TI distro. |
| `[defaults.ti]` | `image` | (none) | Default TI image. |
| `[defaults.ti]` | `manifest` | (none) | Default TI manifest. |
| `[build]` | `container_image` | `jetm/kas-build-env:latest` | kas-container image. Setting it also activates container mode the same way `KAS_CONTAINER_IMAGE` does. |
| `[build]` | `doctor` | `true` | When `false`, skip the pre-flight doctor checks before `build`/`sync` (same as always passing `--skip-doctor`). `--skip-doctor` still works as a per-run override. |
| `[layers]` | `show_hashes` | `false` | When `true`, print each layer's git short hash and branch before `build`/`sync`, equivalent to passing `--show-layers`. |

See `examples/config.toml` for a fully annotated reference to copy to
`~/.config/bspctl/config.toml`.

Workspace is resolved by walking up from CWD for a `.bspctl.toml` marker
or `nxp/`/`ti/` subdirectories. Generic BYO builds skip the walk.

## Common invocations

```bash
bspctl build --image fsl-image-gui      # heavier image (Wayland, Qt6, Chromium)
bspctl build --machine imx93-var-dart   # different SoM
bspctl build --dry-run                  # apply overlay, skip kas-container
bspctl build --skip-sync                # don't re-sync sources/
bspctl build --host                     # bypass kas-container, run plain kas build on the host
bspctl doctor                           # standalone pre-flight
bspctl triage                           # post-mortem latest run
bspctl log                              # tail latest kas.log live
bspctl shell                            # kas-container interactive shell
bspctl shell -c "bitbake -e virtual/kernel | grep ^PREFERRED"
bspctl shell --host                     # plain kas shell on the host (no Docker)
bspctl gen-kas -o nxp/my-build.yml      # write topology YAML only
bspctl clean                            # remove <bsp>/build/
```

## Troubleshooting

| Symptom | Resolution |
|---------|-----------|
| `Not inside a workspace` | cd into a directory with `nxp/` or `ti/`, add a `.bspctl.toml` marker, or pass a positional YAML. |
| `kas YAML is outside bsp_root` | NXP/TI builds need the YAML under `nxp/` or `ti/`. Copy `examples/kas-*.yml` there. Generic BYO is exempt. |
| `Could not parse <path> as a kas build` | YAML lacks both `machine:` and a `repos:` block. |
| `parser thread killed/died` mid-parse | Container Python is 3.13+. Rebuild the kas image on Ubuntu 24.04 (Python 3.12). `bspctl doctor` flags this. |
| `wget: sources.openembedded.org: NXDOMAIN` | Dead mirror in scarthgap. The overlay's `MIRRORS =` line suppresses it. |
| `No space left on device` | `bspctl doctor` blocks below 50 GiB free. Check `df` on workspace, `SSTATE_DIR`, and `DL_DIR`. |
| `do_fetch: Fetcher failure` for `linux-imx` | Populate `nxp/forks/linux-imx/` so the local PREMIRROR serves the fetch. |
| `Config file validation Error` from kas | kas version skew between host and container. Align both. |
| General fetch flake | Retry. `repo sync` and `do_fetch` are idempotent. |
| `workspace not initialized; missing: build/init-build-env` | Run `bitbake-setup init` in the workspace first, then retry `bspctl build`. |
| `no machine selected` in bbsetup mode | Pass `--machine <name>` or add a `machine/<name>` fragment to the bitbake-setup config. |
| `branch "X" does not contain commit "abc..."` from kas | Old kas YAML with both `branch:` and `commit:` set. Regenerate with the current bspctl (pinned repos emit `commit:` only). |
| `local-path sources are out of scope` | `config-upstream.json` contains a source with no `git-remote` key. Remove it or file a bug. |
