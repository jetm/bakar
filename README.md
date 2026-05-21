# bspctl

bspctl is a kas wrapper for Yocto BSP builds. It bridges the gap between
vendor BSP manifests and kas:

Vendor BSPs for NXP i.MX and TI Sitara are distributed as Google repo tool
XML manifests. kas is the modern Yocto build tool - but it speaks its own
YAML, not repo XML. These two worlds don't talk to each other out of the box.

bspctl bridges them:

1. Runs repo tool (or oe-layertool for TI) to populate `sources/` from the
   vendor manifest
2. Translates the manifest XML into a kas YAML (topology only: repos + layers)
3. Layers a curated tuning overlay on top at build time (ccache, mirrors, fetch
   robustness, BSP-specific knobs) without touching your YAML
4. Wraps `kas-container build` with pre-flight checks, structured per-run logs,
   and a post-mortem triage command

| Without bspctl | With bspctl |
|----------------|-------------|
| repo tool XML and kas YAML describe the same thing but don't interoperate | `bspctl gen-kas` translates the vendor XML manifest into a kas YAML |
| Re-run repo tool or oe-layertool by hand on every fresh workspace | `bspctl sync` fetches sources and skips if already current |
| Add ccache, mirror, fetch, and EULA boilerplate to every project YAML | Curated overlay applied at build time; your YAML stays topology-only |
| Start a 4-hour build only to fail on a full disk or wrong container Python | `bspctl doctor` catches environment problems before kas starts |
| Grep through `build/tmp/work/.../temp/log.do_*` to find what failed | `bspctl triage` reads structured run logs, locates the recipe log, and matches against a suggestion table |
| No build history | Per-run directory: structured event log, kas output, env snapshot, disk usage, timing |

Works with any kas YAML in generic mode - the NXP/TI presets are the
batteries-included path, not a requirement.

## Installation

```bash
uv tool install bspctl
```

```bash
pip install bspctl
```

```bash
# From source
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

# BYO YAML (generic or NXP/TI)
bspctl build path/to/my-build.yml
```

On a clean workspace, the first run populates `sources/` via repo sync and
takes hours. Subsequent runs skip sync automatically and go straight to
bitbake.

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

Edit `overlays/bspctl-tuning-<bsp>.yml` to change any knob. The change
applies to BYO and manifest flows alike, with no risk of drift.

### Pre-flight checks

`bspctl doctor` runs before every build (and standalone). BLOCK failures
halt before kas starts; WARN prints and continues.

- Docker daemon reachable and ulimits set
- Container image present locally
- Container Python version (3.13+ deadlocks the bitbake parser fork)
- Disk space on workspace, `SSTATE_DIR`, `DL_DIR`
- NXP: fork repos populated, bitbake override applied
- TI: oe-layertool cloned, active config matches manifest

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

| Family | Manifest pattern | Source sync | Overlay |
|--------|-----------------|-------------|---------|
| **NXP i.MX** | `imx-A.B.C-X.Y.Z.xml` | repo tool | `bspctl-tuning-nxp.yml` |
| **TI Sitara** | `processor-sdk-*-config_var<N>.txt` | oe-layertool | `bspctl-tuning-ti.yml` |
| **Generic** | any kas YAML | none | `bspctl-tuning-generic.yml` |

BSP detection runs on the manifest filename (manifest-driven) or the
YAML's `machine:` value and `repos:` block (BYO). No NXP/TI markers
falls through to generic mode.

## Configuration

CLI flags, env vars, and built-in defaults in that precedence order.

| Env var | Flag | Default |
|---------|------|---------|
| `BSPCTL_MACHINE` | `--machine` | `imx8mp-var-dart` |
| `BSPCTL_DISTRO` | `--distro` | `fsl-imx-xwayland` |
| `BSPCTL_IMAGE` | `--image` | `core-image-minimal` |
| `BSPCTL_MANIFEST` | `--manifest` | `imx-6.6.52-2.2.2.xml` |
| `BSPCTL_REPO_URL` | - | `https://github.com/varigit/variscite-bsp-platform.git` |
| `BSPCTL_REPO_BRANCH` | - | inferred from manifest prefix |
| `KAS_CONTAINER_IMAGE` | - | `jetm/kas-build-env:latest` |
| `SSTATE_DIR` | - | kas default (unset = skip check) |
| `DL_DIR` | - | kas default (unset = skip check) |

Workspace is resolved by walking up from CWD for a `.bspctl.toml` marker
or `nxp/`/`ti/` subdirectories. Generic BYO builds skip the walk.

## Common invocations

```bash
bspctl build --image fsl-image-gui      # heavier image (Wayland, Qt6, Chromium)
bspctl build --machine imx93-var-dart   # different SoM
bspctl build --dry-run                  # apply overlay, skip kas-container
bspctl build --skip-sync                # don't re-sync sources/
bspctl doctor                           # standalone pre-flight
bspctl triage                           # post-mortem latest run
bspctl log                              # tail latest kas.log live
bspctl shell                            # kas-container interactive shell
bspctl shell -c "bitbake -e virtual/kernel | grep ^PREFERRED"
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
