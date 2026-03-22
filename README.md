# varis-build

One-command Variscite BSP build orchestrator for NXP i.MX and TI Sitara SoMs,
with a generic fallback for any kas YAML. Layers a static tuning overlay
(ccache wiring, MIRRORS, PREMIRRORS, FETCHCMD_wget, plus
fork-PREMIRRORs and the renderdoc CMake-launcher fix on NXP) on top of
either a user-supplied kas YAML (BYO) or a YAML it generates from the
Variscite repo-tool manifest (NXP) / oe-layertool config (TI). Runs a
pre-flight diagnosis before every build, captures structured per-run
telemetry under `<bsp_root>/build/runs/<ts>/`, and ships a `varis triage`
post-mortem that keys suggestions off the failure pattern.

## BSP scope

`varis` supports two Variscite BSP families plus a generic mode:

- **NXP i.MX** (i.MX6/7, i.MX8, i.MX8M, i.MX9x, i.MX95) - manifest is
  `imx-A.B.C-X.Y.Z.xml` from `varigit/variscite-bsp-platform`. Layers
  `varis-tuning-nxp.yml` (ccache + MIRRORS + ACCEPT_FSL_EULA + renderdoc
  fix + linux-imx fork PREMIRROR + meta-varis-overrides).
- **TI Sitara** (AM62x, AM62Px, ...) - config is
  `processor-sdk-<poky>-<flavour>-<sdk>-config_var<N>.txt` from
  `varigit/oe-layersetup`. Layers `varis-tuning-ti.yml` (ccache + MIRRORS
  + ti-linux-kernel + ti-u-boot fork PREMIRRORs + meta-varis-overrides-ti).
- **Generic** (any non-Variscite kas YAML, e.g. `qemuarm64` + `poky` +
  `meta-arm`) - no manifest, BYO only. Layers `varis-tuning-generic.yml`
  (the BSP-agnostic subset: ccache, MIRRORS, PREMIRRORS, FETCHCMD_wget,
  PYTHONMALLOC). NXP/TI-specific knobs are deliberately excluded.

The dispatched code path branches on the manifest filename (or the BYO YAML's
machine prefix / repos block) at the top of `varis build`.

## Quickstart

Install `varis` as a uv tool from this directory:

```bash
cd /home/tiamarin/repos/personal/variscite/varis
uv tool install .
```

`varis` lands on PATH. Re-run with `--reinstall` after local edits.

### Python version pinning

`varis` declares `requires-python = ">=3.10,<3.14"`. uv resolves to the
newest interpreter in that range, which on most hosts means 3.13. Some
workflows need to match bitbake's effective production floor (3.10/3.11/
3.12 on poky walnascar; kas-container ships CPython 3.12.10) - in
particular, the `varis stress-parse --host` mode runs bitbake on the
host's interpreter, and bitbake's in-tree `test_parser_fork_race`
auto-skips on 3.14.

Pin the interpreter at install time via `--python <version>`:

```bash
uv python install 3.12
cd /home/tiamarin/repos/personal/variscite/varis
uv tool install --python 3.12 --reinstall --editable .
```

Verify the shebang of the installed entry point points at 3.12:

```bash
head -1 "$(which varis)"
# /home/tiamarin/.local/share/uv/tools/varis-build/bin/python3
"$(head -1 "$(which varis)" | sed 's|^#!||')" --version
# Python 3.12.x
```

Now jump into the workspace and run a default build:

```bash
cd ~/repos/personal/variscite
varis build
```

Defaults are `imx8mp-var-dart` / `fsl-imx-xwayland` / `core-image-minimal` off
the `imx-6.6.52-2.2.2.xml` manifest. On a clean workspace the first run
populates `nxp/sources/` via `repo sync` and takes hours; subsequent runs
skip the sync step and go straight to bitbake.

## The three forms of `varis build`

`varis build` accepts three input shapes that all converge on the same
`kas-container build <yml>:<overlay>` invocation. The optimization stack
applied is the BSP-appropriate slice; the topology comes from your YAML
or from the manifest.

```bash
# Form A: BYO YAML (positional). Skips sync/setup-env/gen-kas.
cp varis/examples/kas-imx95-var-dart.yml nxp/my-build.yml
# (edit nxp/my-build.yml as you wish)
varis bitbake-override --apply
varis build nxp/my-build.yml

# Form A also handles non-Variscite kas YAMLs - the YAML's bsp_root is
# its own parent directory. Generic mode skips bitbake-override.
varis build pilots/0005-hardening/kas.yml

# Form B: manifest-driven, one shot. Variscite-only.
varis bitbake-override --apply
varis build -f imx-6.12.49-2.2.0.xml -m imx95-var-dart
# TI equivalent
varis build -f processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt -m am62x-var-som

# Form C: manifest-driven, staged. Useful when you want to inspect the
# generated YAML before kicking off the build.
varis sync --manifest imx-6.12.49-2.2.0.xml
varis gen-kas --manifest imx-6.12.49-2.2.0.xml -o nxp/my-build.yml
varis build nxp/my-build.yml
```

The user's YAML must live under its `bsp_root` so kas-container can read
it through the `KAS_WORK_DIR` bind mount. For NXP/TI builds that means
under `nxp/` or `ti/`; for generic builds the YAML's own parent
directory is the bsp_root, so any path works. `varis build` errors out
with a clear message if the path is unreachable.

## Overlay model

Every `varis build` invocation - BYO or manifest-driven - applies the
Variscite tuning by layering a static overlay onto the kas YAML at build
time. Your YAML file is not modified on disk; kas merges the overlay in
when it parses the build.

`varis build` copies the overlay shipped under `overlays/` in this
repo into `<bsp_root>/.varis/overlays/varis-tuning-<bsp>.yml` (a real
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

Edit `overlays/varis-tuning-<bsp>.yml` directly when you need to tweak the
optimization stack. The change applies to BYO and manifest flows alike, with
zero risk of drift between the two outputs.

## BSP detection rules

`varis build` classifies the input as NXP, TI, or generic before doing
any work:

- **Form A (BYO YAML)**: inspect the YAML's `machine:` value first
  (`imx*` -> NXP; `am*`, `k3-*`, `j7-*` -> TI), then the `repos:` block
  (`meta-imx`, `meta-freescale*`, `meta-nxp*` -> NXP; `meta-ti-bsp`,
  `meta-ti`, `meta-arago` -> TI). A parseable YAML with a machine or
  repos block but no Variscite markers falls through to **generic**,
  which selects `varis-tuning-generic.yml` and skips the
  bitbake-override step.
- **Form B (manifest)**: regex on the filename (`imx-*.xml` -> NXP;
  `processor-sdk-*-config_var*.txt` or `arago-*.txt` -> TI). Generic
  mode does not apply here - it is BYO-only.

A YAML that lacks both `machine:` and `repos:` (empty / unparseable /
shape-incomplete) exits non-zero with a hint instead of guessing.

## Common invocations

```bash
varis build nxp/my-build.yml             # BYO form (positional YAML)
varis build pilots/0005/kas.yml          # BYO generic mode (non-Variscite)
varis build --image fsl-image-gui        # heavier image (Wayland, Qt6, Chromium)
varis build --machine imx93-var-dart     # different SoM (still NXP)
varis build --dry-run                    # apply overlay, skip kas-container
varis build --skip-sync                  # don't touch sources/, even if stale
varis build --skip-doctor                # bypass pre-flight (not recommended)
varis sync --manifest imx-6.12.49-2.2.0.xml   # repo init+sync, no build
varis doctor                             # standalone diagnosis, no build
varis triage                             # post-mortem the most recent run (workspace BSPs)
varis triage 20260423-091014             # post-mortem a named run
varis triage -k pilots/0005/kas.yml      # post-mortem latest BYO run for that YAML
varis log                                # tail the latest run's kas.log live
varis log pilots/0005/kas.yml            # tail the latest BYO run for that YAML
varis shell                              # drop into a kas-container shell
varis gen-kas -o nxp/my-build.yml        # write topology-only YAML, do nothing else
varis clean                              # remove <bsp>/build/
```

`varis build` is idempotent. Re-running it after a mid-pipeline failure
picks up where it left off - repo sync is skipped if `sources/` is
populated, `setup_env` is skipped if `build/conf/bblayers.conf` exists,
and the topology YAML is regenerated in case flags changed.

## Environment variables

Every `--flag` has a `VARIS_*` env equivalent so CI jobs and shell profiles can
pin defaults without passing args every invocation. Explicit CLI flags
override env vars; env vars override built-in defaults.

| Env var | Field (BuildConfig) | Default |
|---------|---------------------|---------|
| `VARIS_MACHINE` | `machine` | `imx8mp-var-dart` |
| `VARIS_DISTRO` | `distro` | `fsl-imx-xwayland` |
| `VARIS_IMAGE` | `image` | `core-image-minimal` |
| `VARIS_MANIFEST` | `manifest` | `imx-6.6.52-2.2.2.xml` |
| `VARIS_REPO_URL` | `repo_url` | `https://github.com/varigit/variscite-bsp-platform.git` |
| `VARIS_REPO_BRANCH` | `repo_branch` | `scarthgap` |
| `KAS_CONTAINER_IMAGE` | `container_image` | `jetm/kas-build-env:latest` |

`--workspace` has no env var - it is resolved from the current working
directory by walking up to find a `varis/` sibling. The walk is
skipped entirely for generic BYO builds (`varis build my.yml` where
the YAML does not target a Variscite SoM); `bsp_root` falls back to
the YAML's own parent directory so generic builds run from any
location.

Cache locations (`SSTATE_DIR`, `DL_DIR`) are read from the environment and
should be pinned in your shell profile, not as `VARIS_*` vars. See
`kb/reference/bsp-build-environment.md` for the recommended values.

## Pre-flight checks reference

`varis doctor` and the automatic pre-flight gate at the top of `varis build`
run the same checks. BLOCK-severity failures halt the build; WARN prints
and continues; INFO is purely informational. Per-BSP extras
(`check_forks_linux_imx` on NXP; `check_ti_layertool_*` on TI) load via
`BspModel.doctor_extras`; the shared set runs unconditionally. Generic
BYO mode runs only the shared set - the family-specific gates would
always fail outside a Variscite tree and are skipped.

## Observability

Each `varis build` invocation creates `<bsp>/build/runs/<YYYYMMDD-HHMMSS>/`
containing:

| File | Contents |
|------|----------|
| `events.jsonl` | One JSON object per step start/end/error (machine-readable) |
| `console.log` | The same stream in human-readable lines |
| `env.txt` | Snapshot of `VARIS_*`, `KAS_*`, `BB_*`, `DL_*`, `SSTATE_*`, `NPROC`, `MACHINE`, `DISTRO` at run start |
| `kas.log` | `kas-container build` stdout + stderr |
| `time.log` | `/usr/bin/time -v` output (when `time` is available) |
| `du.tsv` | `<unix-ts>\t<bytes>` samples of `build/tmp/` every 30 s |
| `diagnosis.txt` | The pre-flight diagnosis as plain text |

Events use the key `event` (not `event_type`), with values like `run_start`,
`step_start`, `step_ok`, `step_skip`, `step_fail`, `run_end`, `run_error`.

## Triage workflow

By default, `varis triage` searches both `nxp/build/runs/` and
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

`varis log` follows the same convention: pass a positional kas YAML to
tail the latest run for a BYO build, or run from inside a Variscite
workspace to tail the latest run for the dispatched BSP.

## Troubleshooting

| Symptom | Resolution |
|---------|-----------|
| `Not inside a Variscite workspace (looking for varis/)` | cd into a workspace, pass `--workspace`, or pass a positional kas YAML: `varis build|log|triage -k pilots/0005/kas.yml` works from anywhere (generic mode treats the YAML's parent as `bsp_root`). |
| `kas YAML <path> is outside bsp_root <bsp_root>` | NXP/TI builds need the YAML under `nxp/` or `ti/`. Copy `varis/examples/kas-*.yml` under `<bsp_root>/` and re-run. Generic BYO is exempt - the YAML's own directory is `bsp_root`. |
| `Could not parse <path> as a kas build` | YAML lacks both `machine:` and a `repos:` block. Add at least one before re-running. |
| `All concatenated config files must belong to the same repository ...` (kas) | Pre-fix error from the symlinked-overlay era. Reinstall varis at a build that ships the copy-based overlay materializer. |
| `parser thread killed/died` mid-parse | Container Python is 3.13+, which deadlocks the bitbake parser fork. Rebuild `jetm/kas-build-env` on `ubuntu:24.04` (Python 3.12). `varis doctor` flags this. |
| `wget: sources.openembedded.org: NXDOMAIN` noise | Dead mirror hardcoded by scarthgap's `mirrors.bbclass`. The overlay's `MIRRORS =` line suppresses it. |
| `No space left on device` | `varis doctor` blocks below 50 GiB free on workspace, sstate, and downloads. |
| `do_fetch: Fetcher failure` for `linux-imx` | Populate `nxp/forks/linux-imx/` so the local PREMIRROR handles the fetch without going external. |
| `Config file validation Error` from kas | Version skew between host `kas` and the kas-container image. Align both to the same kas release. |
| General fetch flake | Retry. Transient GitHub timeouts are common; `repo sync` and `do_fetch` are idempotent. |

## Architecture

For maintainers: the package lives under `src/varis_build/`.

- `cli.py` - typer app; defines `build`, `sync`, `doctor`, `triage`, `shell`,
  `gen-kas`, `clean`, `bitbake-override`, `stress-parse`, `log`. The
  `_dispatch_bsp` and `_dispatch_from_yaml` helpers route to the right
  `BspModel` based on either the manifest filename or the BYO YAML.
- `config.py` - `BuildConfig` dataclass plus `resolve()` which merges CLI
  flags, `VARIS_*` env vars, and defaults. `cfg.kas_yaml` returns the BYO
  override if set, else `<bsp_root>/kas-<bsp>.yml` (the manifest-flow default).
- `bsp_model.py` - manifest-filename detection (`detect_bsp_family`) plus
  the `BspModel` registry. Each model carries the per-BSP defaults,
  `tuning_overlay_filename`, sync/setup-env steps, and doctor extras.
- `bsp_detect.py` - YAML-content BSP detection used by the BYO path.
- `diagnostics.py` - the pre-flight checks plus `Severity` / `Status` enums
  and `run_all` / `any_blocking_failure` helpers.
- `kas.py` - topology-only YAML generator: takes a manifest XML + bblayers
  template and emits a kas config covering machine/distro/target/repos. The
  Variscite tuning lives in `overlays/varis-tuning-<bsp>.yml` and is layered
  in by `varis build` at run time.
- `observability.py` - `RunLogger` context manager that owns the
  `<bsp>/build/runs/<ts>/` directory and emits structured `step_*` events.
- `triage.py` - `analyse(run_dir, workspace)` post-mortem; returns a
  `TriageReport` with tails and keyed suggestions.
- `workspace.py` - state detection (`needs_repo_sync`, `needs_setup_env`) so
  `varis build` can skip steps that have already run.
- `steps/repo.py`, `steps/ti_layertool.py`, `steps/setup_env.py`,
  `steps/ti_setup_env.py`, `steps/kas_build.py`,
  `steps/bitbake_override.py`, `steps/stress_parse.py` - the pipeline steps.
- `overlays/varis-tuning-nxp.yml`, `overlays/varis-tuning-ti.yml` - the
  static optimization stack layered on top of every build.
- `examples/kas-imx95-var-dart.yml`, `examples/kas-am62x-var-som.yml` -
  starter topology-only YAMLs you can copy under `nxp/` or `ti/` and edit.
