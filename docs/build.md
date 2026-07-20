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
| `--on` | | Dispatch the build to a remote host (ssh alias or `user@ip`) instead of building locally. See [Remote dispatch](#remote-dispatch---on-host). |
| `--yes` | `-y` | Skip the `rsync --delete` confirmation prompt for `--on` dispatch (non-interactive) |

`--host` (bypass kas-container, run plain `kas build` on the host),
`--no-scope` (run the build directly instead of inside a transient systemd
scope; see [Transient systemd scope](#transient-systemd-scope)), and
`--sccache-dist` / `--sccache-scheduler URL` are **global** options handled by the
top-level callback, so they go *before* the subcommand: `bakar --host build ...`,
`bakar --no-scope build ...`, `bakar --sccache-dist build ...`. Placing them after
`build` is rejected with `No such option`.

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
8. **kas-container build** - invokes `kas-container build <kas_yaml>:<overlay>` (or `kas build` in host mode), wrapped in a transient systemd scope by default (see below)

Run telemetry is written to `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/`.

## Transient systemd scope

By default `bakar build` (and the live `bakar bitbake` path) launch the
kas/kas-container invocation inside a transient
`systemd-run --user --scope`. This does two things:

- **Survives session teardown.** The scope is a unit under
  `user@<uid>.service` / `app.slice` - a sibling of the interactive session's
  cgroup, not a child - so closing the terminal, an SSH disconnect, or a
  reaped background shell no longer SIGHUPs the build to death. The scope
  inherits the caller's TTY, environment, and CWD, so the live UI, `kas`,
  `docker`, `sccache`, and every `BAKAR_*`/`KAS_*` env var behave exactly as
  before.
- **Contains a runaway.** The scope's cgroup carries safe resource controls:
  a `MemoryMax` hard ceiling **below** total RAM (a memory blow-up
  OOM-kills the build cgroup instead of driving the whole box into an OOM
  storm), a `MemoryHigh` soft reclaim throttle, a positive `oom_score_adj`
  so the build is the OOM victim under *global* pressure (protecting PID 1
  and the desktop), and below-default `CPUWeight`/`IOWeight` to keep the host
  responsive under contention. Build parallelism (`BB_NUMBER_THREADS`,
  `PARALLEL_MAKE`) is deliberately **not** capped.

The unit name is stable per workspace+target and its lifecycle (start/stop,
OOM kills) is visible with `journalctl --user -u <unit>` (the run log prints
the exact command). The run log (`kas.log`) is written as before.

Host vs container mode: in host mode kas runs bitbake directly under the
scope, so the memory ceiling genuinely caps the build. In container mode the
heavy work runs inside the `docker`/`podman` container, whose processes live
in the runtime's cgroup - the scope there delivers session-survival but the
memory ceiling only bounds the lightweight `kas-container`/`docker` client.

Tune the limits under `[build] scope*` in `config.toml` (see
[config-reference.md](config-reference.md)); disable per-invocation with the
global `--no-scope` (before the subcommand: `bakar --no-scope build ...`), or
permanently with `bakar settings set build.scope false`. When `systemd-run`
or the user manager is unavailable (e.g. a minimal CI container), bakar warns
once and runs the build unwrapped.

This hardening addresses build **session-survival and resource containment
only**. It does not prevent filesystem/kernel faults (e.g. an XFS root-fs
corruption panic), which are tracked separately - no cgroup control changes
that class of failure.

## Remote dispatch (`--on <host>`)

`--on <host>` runs the whole build on an idle remote node instead of the local
machine. It mirrors your working tree (uncommitted edits included) to the remote,
runs `bakar build` there, streams the output back live, and prints the remote
run-id so you can triage failures over ssh. Every other build flag passes through
unchanged. When `--on` is unset the build is byte-identical to a local run - no
ssh or rsync is spawned.

`<host>` is any ssh destination: an alias from `~/.ssh/config` or a `user@ip`.
The remote must have the workspace checked out at the same absolute path as the
local machine and run a matching `bakar` version.

```bash
# Dispatch the build to the pc2 node; prompts before the destructive sync
bakar build meta-avocado/kas/machine/qemux86-64.yml --on pc2

# Non-interactive: skip the sync confirmation prompt
bakar build meta-avocado/kas/machine/qemux86-64.yml --on pc2 --yes

# Force the remote build to join the sccache-dist cluster (off by default)
bakar --sccache-dist build meta-avocado/kas/machine/qemux86-64.yml --on pc2 --yes
```

### Dispatch sequence

1. **Host preflight** - `ssh -o BatchMode=yes <host> bash -s` runs
   `command -v bakar && bakar --version` (the same non-login bash the build
   uses). If the host is unreachable, key auth is not set up, or `bakar` is not
   on the remote non-login PATH, the command fails fast with a clear message and
   spawns no rsync. A local/remote `bakar --version` mismatch prints a warning
   but does not block.
2. **Working-tree mirror** - `rsync -a --delete` copies the workspace root to the
   same absolute path on the remote. rsync carries uncommitted edits verbatim
   (`git push` cannot), and the existing remote clone makes this a fast delta.
   `.git` is kept (kas/bitbake read git state for `SRCREV`/`AUTOREV`); build
   artifacts and caches are excluded. Workspace-root outputs are anchored with a
   leading `/` so an unanchored basename cannot also drop a real source dir
   (e.g. oe-core's `meta/recipes-devtools/ccache/`): `/build/`, `/build-*/`,
   `/*/build/`, `/ccache/`, plus the depth-matched `**/tmp/`, `**/sstate-cache/`,
   `**/downloads/`, `**/.venv/`, `**/__pycache__/`, and `**/*.pyc`. NFS-mounted
   shared caches live outside the workspace, so they are never in scope.
3. **Delete confirmation** - because `--delete` mutates the remote irreversibly,
   bakar first runs an `rsync --delete --dry-run -i` preview and prompts for
   confirmation before any real transfer. `--yes` (`-y`) bypasses the prompt for
   non-interactive or scripted runs.
4. **Remote build** - the build runs on the remote with sccache-dist forced
   **off** by default (`BAKAR_SCCACHE_DIST=0`), so a node that is normally an
   sccache-dist server runs as an independent worker (local ccache + shared
   sstate) rather than re-coupling to the cluster. Pass `--sccache-dist` to opt
   back in - the forwarded flag wins over the env default by CLI-over-env
   precedence. The remote command is delivered fish-safely as a script over
   stdin to `ssh <host> bash -s` (the remote login shell is fish, which mangles
   a naive `ssh <host> '<cmd>'` or `bash -lc` invocation).
5. **Live streaming + run-id** - the remote build output streams to your terminal
   as it runs. On completion bakar prints the remote run-id and a copy-pasteable
   `ssh <host> bakar triage <run-id>` line so you (or Claude, over ssh) can
   inspect the run remotely.
6. **Exit propagation** - a non-zero remote build exits the local command with
   the same code; the failure message points at the remote triage command.

A single `--on` dispatch opens up to four ssh connections (preflight, rsync's
own ssh, the build session, and the run-id discovery find). On a high-latency
link, a `~/.ssh/config` entry with `ControlMaster auto` and a `ControlPersist`
window amortizes them onto one shared connection.

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
