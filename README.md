# bspctl

Practical kas wrapper for Yocto BSP development. Integrates repo tool
(NXP i.MX manifests) and oe-layertool (TI Sitara) for source sync, then
drives kas with a static tuning overlay (ccache, MIRRORS, PREMIRRORS,
fetch robustness) layered on top. Works with any kas YAML in generic
mode. Runs pre-flight checks before every build, captures structured
per-run telemetry, and ships `bspctl triage` to diagnose failures from
the kas log.

## BSP scope

`bspctl` supports two BSP families plus a generic mode:

- **NXP i.MX** (i.MX6/7, i.MX8, i.MX8M, i.MX9x, i.MX95) - manifest is
  `imx-A.B.C-X.Y.Z.xml` from `varigit/variscite-bsp-platform`. Layers
  `bspctl-tuning-nxp.yml` (ccache + MIRRORS + ACCEPT_FSL_EULA + renderdoc
  fix + linux-imx fork PREMIRROR + meta-varis-overrides).
- **TI Sitara** (AM62x, AM62Px, ...) - config is
  `processor-sdk-<poky>-<flavour>-<sdk>-config_var<N>.txt` from
  `varigit/oe-layersetup`. Layers `bspctl-tuning-ti.yml` (ccache + MIRRORS
  + ti-linux-kernel + ti-u-boot fork PREMIRRORs + meta-varis-overrides-ti).
- **Generic** (any non-NXP/TI kas YAML, e.g. `qemuarm64` + `poky` +
  `meta-arm`) - no manifest, BYO only. Layers `bspctl-tuning-generic.yml`
  (the BSP-agnostic subset: ccache, MIRRORS, PREMIRRORS, FETCHCMD_wget,
  PYTHONMALLOC). NXP/TI-specific knobs are deliberately excluded.

The dispatched code path branches on the manifest filename (or the BYO YAML's
machine prefix / repos block) at the top of `bspctl build`.

## Installation

```bash
uv tool install bspctl
```

Or with pip:

```bash
pip install bspctl
```

### From source (for contributors)

```bash
git clone https://github.com/jetm/bspctl
cd bspctl
uv tool install --editable .
```

## Quickstart

Install `bspctl` as a uv tool from this directory:

```bash
cd ~/repos/personal/bspctl
uv tool install .
```

`bspctl` lands on PATH. Re-run with `--reinstall` after local edits.

### Python version pinning

`bspctl` declares `requires-python = ">=3.11,<3.14"`. uv resolves to the
newest interpreter in that range, which on most hosts means 3.13. Some
workflows need to match bitbake's effective production floor (3.11/3.12
on poky walnascar; kas-container ships CPython 3.12.10) - in
particular, the `bspctl stress-parse --host` mode runs bitbake on the
host's interpreter, and bitbake's in-tree `test_parser_fork_race`
auto-skips on 3.14.

Pin the interpreter at install time via `--python <version>`:

```bash
uv python install 3.12
cd ~/repos/personal/bspctl
uv tool install --python 3.12 --reinstall --editable .
```

Verify the shebang of the installed entry point points at 3.12:

```bash
head -1 "$(which bspctl)"
# /home/user/.local/share/uv/tools/bspctl/bin/python3
"$(head -1 "$(which bspctl)" | sed 's|^#!||')" --version
# Python 3.12.x
```

Now jump into the workspace and run a default build:

```bash
cd ~/bsp-workspace
bspctl build
```

Defaults are `imx8mp-var-dart` / `fsl-imx-xwayland` / `core-image-minimal` off
the `imx-6.6.52-2.2.2.xml` manifest. On a clean workspace the first run
populates `nxp/sources/` via `repo sync` and takes hours; subsequent runs
skip the sync step and go straight to bitbake.

## The three forms of `bspctl build`

`bspctl build` accepts three input shapes that all converge on the same
`kas-container build <yml>:<overlay>` invocation. The optimization stack
applied is the BSP-appropriate slice; the topology comes from your YAML
or from the manifest.

```bash
# Form A: BYO YAML (positional). Skips sync/setup-env/gen-kas.
cp bspctl/examples/kas-imx95-var-dart.yml nxp/my-build.yml
# (edit nxp/my-build.yml as you wish)
bspctl bitbake-override --apply
bspctl build nxp/my-build.yml

# Form A also handles generic kas YAMLs - the YAML's bsp_root is
# its own parent directory. Generic mode skips bitbake-override.
bspctl build pilots/0005-hardening/kas.yml

# Form B: manifest-driven, one shot. NXP/TI only.
bspctl bitbake-override --apply
bspctl build -f imx-6.12.49-2.2.0.xml -m imx95-var-dart
# TI equivalent
bspctl build -f processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt -m am62x-var-som

# Form C: manifest-driven, staged. Useful when you want to inspect the
# generated YAML before kicking off the build.
bspctl sync --manifest imx-6.12.49-2.2.0.xml
bspctl gen-kas --manifest imx-6.12.49-2.2.0.xml -o nxp/my-build.yml
bspctl build nxp/my-build.yml
```

The user's YAML must live under its `bsp_root` so kas-container can read
it through the `KAS_WORK_DIR` bind mount. For NXP/TI builds that means
under `nxp/` or `ti/`; for generic builds the YAML's own parent
directory is the bsp_root, so any path works. `bspctl build` errors out
with a clear message if the path is unreachable.

## Overlay model

Every `bspctl build` invocation - BYO or manifest-driven - applies the
BSP tuning by layering a static overlay onto the kas YAML at build
time. Your YAML file is not modified on disk; kas merges the overlay in
when it parses the build.

`bspctl build` copies the overlay shipped under `overlays/` in this
repo into `<bsp_root>/.bspctl/overlays/bspctl-tuning-<bsp>.yml` (a real
file, refreshed on every invocation so it tracks the source) and then
runs:

```text
kas-container build <user-yml-rel>:<overlay-rel>
```

The copy lands inside whatever git repo (or no repo) hosts your YAML,
so kas's "all concatenated config files must share a repo" check is
satisfied, and kas-container reads it directly through the
`KAS_WORK_DIR` bind mount with no symlink to dereference.

kas merges `local_conf_header.<key>` and `repos.<name>` by name, so your
`machine:`, `distro:`, `target:`, and own `repos:` stay intact while the
overlay unions in:

- ccache wiring (`CCACHE_DIR`, `INHERIT += "ccache"`)
- thread/parallelism knobs (`BB_NUMBER_THREADS`, `PARALLEL_MAKE`)
- `IMAGE_FEATURES:remove` for dev/dbg packages
- `BB_FETCH_TIMEOUT = "600"` (raise the per-URI timeout)
- `MIRRORS` (replace scarthgap's dead mirror with the Yocto Project mirror)
- `PREMIRRORS:prepend` for github.com (silent fallbacks instead of build blockers)
- `FETCHCMD_wget` (crates.io 403 workaround)
- NXP-only: `ACCEPT_FSL_EULA`, the renderdoc CMake-launcher fix
- Per-BSP fork PREMIRRORs (`forks/linux-imx` on NXP; `forks/ti-linux-kernel`
  + `forks/ti-u-boot` on TI)
- The `meta-varis-overrides` (NXP) / `meta-varis-overrides-ti` (TI) carry
  layer

Edit `overlays/bspctl-tuning-<bsp>.yml` directly when you need to tweak the
optimization stack. The change applies to BYO and manifest flows alike, with
zero risk of drift between the two outputs.

## BSP detection rules

`bspctl build` classifies the input as NXP, TI, or generic before doing
any work:

- **Form A (BYO YAML)**: inspect the YAML's `machine:` value first
  (`imx*` -> NXP; `am*`, `k3-*`, `j7-*` -> TI), then the `repos:` block
  (`meta-imx`, `meta-freescale*`, `meta-nxp*` -> NXP; `meta-ti-bsp`,
  `meta-ti`, `meta-arago` -> TI). A parseable YAML with a machine or
  repos block but no NXP/TI markers falls through to **generic**,
  which selects `bspctl-tuning-generic.yml` and skips the
  bitbake-override step.
- **Form B (manifest)**: regex on the filename (`imx-*.xml` -> NXP;
  `processor-sdk-*-config_var*.txt` or `arago-*.txt` -> TI). Generic
  mode does not apply here - it is BYO-only.

A YAML that lacks both `machine:` and `repos:` (empty / unparseable /
shape-incomplete) exits non-zero with a hint instead of guessing.

## Common invocations

```bash
bspctl build nxp/my-build.yml             # BYO form (positional YAML)
bspctl build pilots/0005/kas.yml          # BYO generic mode
bspctl build --image fsl-image-gui        # heavier image (Wayland, Qt6, Chromium)
bspctl build --machine imx93-var-dart     # different SoM (still NXP)
bspctl build --dry-run                    # apply overlay, skip kas-container
bspctl build --skip-sync                  # don't touch sources/, even if stale
bspctl build --skip-doctor                # bypass pre-flight (not recommended)
bspctl sync --manifest imx-6.12.49-2.2.0.xml   # repo init+sync, no build
bspctl doctor                             # standalone diagnosis, no build
bspctl triage                             # post-mortem the most recent run (workspace BSPs)
bspctl triage 20260423-091014             # post-mortem a named run
bspctl triage -k pilots/0005/kas.yml      # post-mortem latest BYO run for that YAML
bspctl log                                # tail the latest run's kas.log live
bspctl log pilots/0005/kas.yml            # tail the latest BYO run for that YAML
bspctl shell                              # drop into a kas-container shell
bspctl gen-kas -o nxp/my-build.yml        # write topology-only YAML, do nothing else
bspctl clean                              # remove <bsp>/build/
```

`bspctl build` is idempotent. Re-running it after a mid-pipeline failure
picks up where it left off - repo sync is skipped if `sources/` is
populated, `setup_env` is skipped if `build/conf/bblayers.conf` exists,
and the topology YAML is regenerated in case flags changed.

## Environment variables

Every `--flag` has a `BSPCTL_*` env equivalent so CI jobs and shell profiles can
pin defaults without passing args every invocation. Explicit CLI flags
override env vars; env vars override built-in defaults.

| Env var | Field (BuildConfig) | Default |
|---------|---------------------|---------|
| `BSPCTL_MACHINE` | `machine` | `imx8mp-var-dart` |
| `BSPCTL_DISTRO` | `distro` | `fsl-imx-xwayland` |
| `BSPCTL_IMAGE` | `image` | `core-image-minimal` |
| `BSPCTL_MANIFEST` | `manifest` | `imx-6.6.52-2.2.2.xml` |
| `BSPCTL_REPO_URL` | `repo_url` | `https://github.com/varigit/variscite-bsp-platform.git` |
| `BSPCTL_REPO_BRANCH` | `repo_branch` | `scarthgap` |
| `KAS_CONTAINER_IMAGE` | `container_image` | `jetm/kas-build-env:latest` |

`--workspace` has no env var - it is resolved from the current working
directory by walking up to find a `.bspctl.toml` marker file or `nxp/`/`ti/`
subdirectories. The walk is skipped entirely for generic BYO builds
(`bspctl build my.yml` where the YAML does not target an NXP/TI SoM);
`bsp_root` falls back to the YAML's own parent directory so generic builds
run from any location.

Cache locations (`SSTATE_DIR`, `DL_DIR`) are read from the environment and
should be pinned in your shell profile.

## Pre-flight checks reference

`bspctl doctor` and the automatic pre-flight gate at the top of `bspctl build`
run the same checks. BLOCK-severity failures halt the build; WARN prints
and continues; INFO is purely informational. Per-BSP extras
(`check_forks_linux_imx` on NXP; `check_ti_layertool_*` on TI) load via
`BspModel.doctor_extras`; the shared set runs unconditionally. Generic
BYO mode runs only the shared set - the family-specific gates would
always fail outside an NXP/TI workspace and are skipped.

## Observability

Each `bspctl build` invocation creates `<bsp>/build/runs/<YYYYMMDD-HHMMSS>/`
containing:

| File | Contents |
|------|----------|
| `events.jsonl` | One JSON object per step start/end/error (machine-readable) |
| `console.log` | The same stream in human-readable lines |
| `env.txt` | Snapshot of `BSPCTL_*`, `KAS_*`, `BB_*`, `DL_*`, `SSTATE_*`, `NPROC`, `MACHINE`, `DISTRO` at run start |
| `kas.log` | `kas-container build` stdout + stderr |
| `time.log` | `/usr/bin/time -v` output (when `time` is available) |
| `du.tsv` | `<unix-ts>\t<bytes>` samples of `build/tmp/` every 30 s |
| `diagnosis.txt` | The pre-flight diagnosis as plain text |

Events use the key `event` (not `event_type`), with values like `run_start`,
`step_start`, `step_ok`, `step_skip`, `step_fail`, `run_end`, `run_error`.

## Triage workflow

By default, `bspctl triage` searches both `nxp/build/runs/` and
`ti/build/runs/` under the workspace. Pass `-k <my.yml>` (or
`--kas-yaml`) for a BYO build whose runs live next to the YAML at
`<yaml-parent>/build/runs/`. In either mode, it finds the named run
(or the most recent), reads `events.jsonl` to locate the first
`step_fail` event, tails `kas.log`, extracts the container path of
bitbake's "Logfile of failure stored in: ..." line and rewrites it to
the host path, tails that recipe log, and matches the combined output
against a keyed suggestion table (fetch failure, parser deadlock, OOM,
disk full, GitHub fetch flake, missing EULA, stale bitbake cache,
kas/container version skew).

`bspctl log` follows the same convention: pass a positional kas YAML to
tail the latest run for a BYO build, or run from inside an NXP/TI
workspace to tail the latest run for the dispatched BSP.

## Troubleshooting

| Symptom | Resolution |
|---------|-----------|
| `Not inside a workspace (no .bspctl.toml found, and no nxp/ or ti/ subdirectory)` | cd into a workspace that contains `nxp/` or `ti/`, or add a `.bspctl.toml` marker file, or pass a positional kas YAML: `bspctl build|log|triage -k pilots/0005/kas.yml` works from anywhere (generic mode treats the YAML's parent as `bsp_root`). |
| `kas YAML <path> is outside bsp_root <bsp_root>` | NXP/TI builds need the YAML under `nxp/` or `ti/`. Copy `bspctl/examples/kas-*.yml` under `<bsp_root>/` and re-run. Generic BYO is exempt - the YAML's own directory is `bsp_root`. |
| `Could not parse <path> as a kas build` | YAML lacks both `machine:` and a `repos:` block. Add at least one before re-running. |
| `All concatenated config files must belong to the same repository ...` (kas) | Pre-fix error from the symlinked-overlay era. Reinstall bspctl at a build that ships the copy-based overlay materializer. |
| `parser thread killed/died` mid-parse | Container Python is 3.13+, which deadlocks the bitbake parser fork. Rebuild `jetm/kas-build-env` on `ubuntu:24.04` (Python 3.12). `bspctl doctor` flags this. |
| `wget: sources.openembedded.org: NXDOMAIN` noise | Dead mirror hardcoded by scarthgap's `mirrors.bbclass`. The overlay's `MIRRORS =` line suppresses it. |
| `No space left on device` | `bspctl doctor` blocks below 50 GiB free on workspace, sstate, and downloads. |
| `do_fetch: Fetcher failure` for `linux-imx` | Populate `nxp/forks/linux-imx/` so the local PREMIRROR handles the fetch without going external. |
| `Config file validation Error` from kas | Version skew between host `kas` and the kas-container image. Align both to the same kas release. |
| General fetch flake | Retry. Transient GitHub timeouts are common; `repo sync` and `do_fetch` are idempotent. |
