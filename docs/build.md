# bakar build

Run the full BSP build pipeline: doctor checks, source sync, kas YAML generation, and kas-container build.

## Synopsis

```text
bakar build [KAS_YAML] [OPTIONS]
```

## Forms

### BYO (bring your own YAML)

Pass a kas YAML directly. Sync, setup-env, and gen-kas are skipped; bakar applies the static tuning overlay and runs kas-container.

```bash
bakar build my-board.yml
bakar build kas/main.yml:kas/overlay.yml    # colon-separated overlay stack
```

### Manifest-driven (NXP / TI)

Supply a manifest filename. bakar runs `repo init+sync` (NXP) or `oe-layertool populate` (TI), then generates the kas YAML, applies the overlay, and builds.

```bash
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart
bakar build -f processor-sdk-10.1.0.8-config_var1.txt -m am62x-var-som
```

### bitbake-setup workspace

When no YAML or manifest is given and the CWD (or `--workspace`) is a bitbake-setup workspace, bakar auto-detects it and translates `config/config-upstream.json` into a kas YAML before building.

```bash
cd ~/bsp/my-bbsetup-ws && bakar build
bakar build -m imx8mp-var-dart    # machine override in bbsetup workspace
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--machine` | `-m` | Target machine (e.g. `imx8mp-var-dart`, `am62x-var-som`) |
| `--distro` | `-d` | Distro (e.g. `fsl-imx-xwayland`, `arago`) |
| `--image` | `-i` | Image target (e.g. `core-image-minimal`, `var-thin-image`) |
| `--target` | `-t` | kas target override (`kas build --target <TARGET>`, e.g. `avocado-complete`); unset builds the YAML's own target. Pair with `SDKMACHINE=<arch>` for SDK targets. |
| `--manifest` | `-f` | Manifest filename (NXP `.xml` or TI `.txt`) |
| `--branch` | `-b` | Branch override (inferred from manifest when omitted) |
| `--skip-sync` | | Skip repo/layertool sync step |
| `--keep-going` | `-k` | Pass `-k` to bitbake: continue building other targets when one fails |
| `--dry-run` | `-n` | Regenerate YAML and exit before invoking kas-container |
| `--dry-run-script` | | Write a runnable bash script to PATH instead of building; use `-` for stdout. Distinct from `--dry-run`: this produces an executable script, not a preview. |
| `--clean` | | Remove `<bsp>/build/` before running (forces from-scratch build) |
| `--show-layers` | | Print layer git hashes before the build starts |
| `--sstate-mirror` | | HTTP sstate/downloads mirror URL; activates `bakar-tuning-shared-cache.yml` for this build |
| `--preset` | | Named preset from `config.toml`; additive with explicit flags (explicit flags win) |
| `--workspace` | `-w` | Workspace root override |

`--host` (bypass kas-container, run plain `kas build` on the host) and
`--sccache-dist` / `--sccache-scheduler URL` are **global** options handled by the
top-level callback, so they go *before* the subcommand: `bakar --host build ...`,
`bakar --sccache-dist build ...`. Placing them after `build` is rejected with
`No such option`.

**Global option:** `--hide-doctor-report`, placed before the subcommand
(`bakar --hide-doctor-report build ...`), runs the doctor checks but prints
output only for build-blocking issues. Set `[build] show_doctor_report = false`
for the same effect on every invocation. Doctor checks always run - a blocking
failure aborts the build whether or not the report is shown.

## Examples

```bash
# Minimal NXP build (machine required, distro/image from defaults or config.toml)
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# NXP build with explicit image, skip sync (sources already present)
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart -i var-thin-image --skip-sync

# NXP from-scratch build (wipe build/ first)
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --clean

# Dry-run: regenerate YAML and show what would run, don't invoke kas-container
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run

# TI Sitara build
bakar build -f processor-sdk-10.1.0.8-config_var1.txt -m am62x-var-som

# BYO: build a hand-crafted kas YAML without any sync
bakar build my-project.yml

# Override the kas target (e.g. build the avocado SDK feed instead of the
# YAML's default distro target); SDKMACHINE picks the SDK arch
SDKMACHINE=x86_64 bakar build meta-avocado/kas/machine/qemux86-64.yml --target avocado-complete

# BYO with colon-separated overlay
bakar build kas/main.yml:kas/sstate-mirror.yml

# Show layer hashes before building (confirm sources are what you expect)
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --show-layers

# Host mode (skip kas-container, run kas directly - requires host Yocto prereqs)
# --host is global: it goes before the subcommand
bakar --host build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Pull sstate and downloads from a team mirror (activates shared-cache overlay)
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --sstate-mirror https://cache.example.com

# Emit a runnable script to stdout (NXP: script contains repo init/sync sync step)
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run-script -

# Write the script to a file instead of stdout
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run-script build.sh

# BYO workspace: script contains kas-container checkout sync step
bakar build my-board.yml --dry-run-script -
```

## What happens

1. **Doctor** - always runs pre-flight checks; `--hide-doctor-report` or `build.show_doctor_report = false` hides the report (only build-blocking issues print). A BLOCK-severity failure still aborts the build.
2. **Sync** (manifest-driven only) - `repo init+sync` for NXP, `oe-layertool populate` for TI; skipped if already up to date or `--skip-sync`
3. **setup-env** (manifest-driven only) - runs `var-setup-release.sh` or local.conf fixup; skipped if `bblayers.conf` already present
4. **bitbake-override** - swaps the BSP-bundled bitbake for a local upstream checkout
5. **gen-kas** (manifest-driven only) - regenerates `kas-<bsp>.yml` from the manifest
6. **hashserv** - when `[build] hashserv = true`, ensures the workspace-scoped bitbake-hashserv daemon is running, injects `BB_HASHSERVE` into the container env, AND auto-appends `bakar-tuning-hashequiv.yml` to the overlay list so `BB_SIGNATURE_HANDLER = "OEEquivHash"` takes effect with no extra user wiring. See [hashserv.md](hashserv.md).
7. **shared-cache overlay** - when `--sstate-mirror <URL>` is passed (or `sstate_mirror_url` is set in config.toml), bakar exports `BAKAR_SSTATE_MIRROR_URL` into the container env and appends `bakar-tuning-shared-cache.yml` to the overlay list. That overlay wires `SSTATE_MIRRORS` (using the `/all/PATH;downloadfilename=PATH` layout required by the Yocto Project autobuilder convention) and `BB_HASHSERVE_UPSTREAM` to enable sstate reuse from the mirror. The official Yocto Project mirror is `http://sstate.yoctoproject.org`. No hand-edited YAML needed.
8. **kas-container build** - invokes `kas-container build <kas_yaml>:<overlay>` (or `kas build` in host mode)

Run telemetry is written to `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/`.

## On failure

```bash
bakar triage                        # inspect the most recent failed run
bakar triage 20260601-143022        # inspect a specific run
```

## See also

- [sync.md](sync.md) - sync sources without building
- [doctor.md](doctor.md) - run pre-flight checks standalone
- [triage.md](triage.md) - post-mortem a failed build
- [configuration.md](configuration.md) - env vars and config.toml defaults
- [hashserv.md](hashserv.md) - persistent hashserv daemon auto-started during step 6
