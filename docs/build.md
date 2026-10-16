# bakar build

Run the full BSP build pipeline: doctor checks, source sync, kas YAML generation, and the kas build.

The build runs `kas` directly on the host by default. Pass the global
`--container` flag (or set `BAKAR_CONTAINER=1`, or a `[build] container`
toggle) to route it through `kas-container` instead.

## Synopsis

```text
bakar build [KAS_YAML] [OPTIONS]
```

## Forms

### BYO (bring your own YAML)

Pass a kas YAML directly. Sync, setup-env, and gen-kas are skipped; bakar applies the static tuning overlay and runs kas.

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
| `--dry-run` | `-n` | Regenerate YAML and exit before invoking kas |
| `--dry-run-script` | | Write a runnable bash script to PATH instead of building; use `-` for stdout. Distinct from `--dry-run`: this produces an executable script, not a preview. |
| `--clean` | | Remove `<bsp>/build/` before running (forces from-scratch build) |
| `--show-layers` | | Print layer git hashes before the build starts |
| `--sstate-mirror` | | HTTP sstate/downloads mirror URL; activates `bakar-tuning-shared-cache.yml` for this build |
| `--preset` | | Named preset from `config.toml`; additive with explicit flags (explicit flags win) |
| `--workspace` | `-w` | Workspace root override |
| `--on` | | Dispatch the build to a remote host (ssh alias or `user@ip`) instead of building locally. See [Remote dispatch](#remote-dispatch---on-host). |
| `--yes` | `-y` | Proceed despite a local/remote bakar version mismatch for `--on` dispatch. Dispatch has no other prompt or override: it prints its plan and never deletes anything on the remote |
| `--no-capture-graph` | | Skip the post-build `bitbake -g` dependency-graph capture, including the cooker-idle wait before it. Same effect as `[build] capture_graph = false` |

`--container` (route the build through `kas-container` instead of the default
host `kas build`), `--host` (retained no-op alias that forces the already-default
host path),
`--no-scope` (run the build directly instead of inside a transient systemd
scope; see [Transient systemd scope](#transient-systemd-scope)), and
`--sccache-dist` / `--sccache-scheduler URL` are **global** options handled by the
top-level callback, so they go *before* the subcommand: `bakar --container build ...`,
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

# Dry-run: regenerate YAML and show what would run, don't invoke kas
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

# Container mode (route through kas-container instead of the default host kas)
# --container is global: it goes before the subcommand
bakar --container build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

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
8. **kas build** - invokes `kas build <kas_yaml>:<overlay>` on the host (or `kas-container build` when the container path is opted into), wrapped in a transient systemd scope by default (see below)

Run telemetry is written to `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/`.

## Native signature capture

After every `bakar build` and `bakar bitbake` run that reached bitbake, bakar
records what the run built so a later `bakar insights --natives` can explain
why native and cross recipes rebuilt, and so `bakar doctor` can forecast the
next one. Two things are written under the effective sstate directory
(`SSTATE_DIR` from the environment wins over `[build] sstate_dir`):

| Path | Content |
|------|---------|
| `<sstate_dir>/.bakar/native-provenance/<release>/<digest>.json` | The revision set of the workspace repositories for this build, one file per distinct revision set, with the last run and node that built it |
| `<sstate_dir>/.bakar/native-sigdata/<recipe>/<task>.<hash>.sigdata` | A copy of the signature file of each native or cross task the run executed |

The run directory also gets a `native-signatures.json` manifest naming the
recipe, task and hash of each executed native or cross task, which is what
`insights --natives` reads for the current run. The revision records feed the
`native-rebuild-forecast` check in [doctor.md](doctor.md#native-rebuild-forecast).

The capture is best-effort and never changes the exit code. When the ledger
cannot be written, one yellow warning line says so
(`native signature ledger could not be written` or
`native revision record could not be written`) and the build result stands.
With no effective sstate directory the record and the ledger are skipped
without any message, and no `native-signatures.json` is written either; the
summary line below does not depend on the sstate directory.

Both directories expire through the age sweep of `bakar clean-cache`, not on
their own. The sweep's time basis is access time on mounts that track it and
modification time otherwise, the same as for the rest of the sstate directory.

The revision records and the signature ledger live under `<sstate_dir>/.bakar/`,
the same directory that holds the hash-equivalence and PR-server daemon state.
With a node-local `.bakar` (a bind mount or a node-local sstate directory) the
records and the ledger stay on each node, so the pre-build forecast only knows
about builds made on that node. The ledger and records are written only into
real directories: a symlinked `.bakar`, `native-provenance` or release
directory is refused, and the record is skipped with a warning.

`bakar build` (not `bakar bitbake`) ends with one line when at least one
native or cross task executed:

```text
native/cross: 412 tasks executed, 96 restored from sstate - bakar insights --natives 20260601-143022-4321 explains why
```

A build where every native and cross task was restored prints nothing.

## Cache mount gate

Immediately before step 8 (`kas build`), and before any bitbake process starts,
bakar probes every effective cache directory (sstate, downloads, ccache) with a
20-second bounded readiness check. This runs on every build - it is not gated
by `[build] hashserv`, `--sstate-mirror`, or any other opt-in flag - and it
deliberately triggers an idle `systemd.automount` unit along the way, so a
cache share that auto-unmounted since the last build is live again before
bitbake ever parses.

If a cache directory is unresponsive, errored, or declared NFS in `/etc/fstab`
but resolved to local disk, the build refuses before `kas`/`bitbake` is ever
invoked, naming the unusable directory and its server. This is a separate,
unconditional check from the doctor `cache-mounts` check in step 1: doctor
reports WARN/BLOCK ahead of the pipeline, while this gate is the last word
right before the process launches. See [bitbake.md](bitbake.md#cache-mount-gate)
for the full detail and an example refusal message.

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
- **Keeps the host responsive (by default).** The scope sets a positive
  `oom_score_adj` so the build is the OOM victim under *global* pressure
  (protecting PID 1 and the desktop). Build parallelism
  (`BB_NUMBER_THREADS`, `PARALLEL_MAKE`) is never touched.

  `CPUWeight`/`IOWeight` (`[build] scope_cpu_weight`/`scope_io_weight`) are
  **off by default (`0`)**. They are opt-in because setting them makes systemd
  realize the cpu/io cgroup controllers across the whole `app.slice` hierarchy,
  not just this scope; under a heavy-I/O recipe the io controller's
  proportional throttling can hang the whole session (a stall with no OOM and
  no panic - observed with chromium). Enable them only where measured
  contention justifies the risk.

**Memory ceilings are OFF by default.** `MemoryHigh`/`MemoryMax` are opt-in
(`[build] scope_memory_high`/`scope_memory_max`, both `0.0` = off by default).
On a host with a large zram/zswap swap they do more harm than good:
`MemoryMax` (`memory.max`) caps only RAM-resident memory, so crossing it
spills the build's pages into swap - and zram stores them *compressed in RAM*,
so the "ceiling" never bounds physical RAM. `MemoryHigh` then just forces
direct reclaim on the build's anon-heavy (unswappable) working set, spinning
CPUs; on a box booted with `softlockup_panic=1` that can panic the whole
machine, and on any workstation it swap-thrashes the desktop. Enable them only
on a *dedicated* build host where OOM-killing the build to protect the host is
the goal - when `scope_memory_max` is set the scope also emits
`MemorySwapMax=0` so the cap becomes a real RAM ceiling (a clean cgroup-OOM,
not a zram thrash). With the ceilings off (the default) the build's memory
behaves exactly as it did before the scope existed.

The unit name is stable per workspace+target and its lifecycle (start/stop,
resource accounting, OOM kills) is visible with `journalctl --user -u <unit>`
(the run log prints the exact command). Use the unit name verbatim: it ends in
`.scope`, and `systemctl`/`journalctl` resolve an unsuffixed name to `.service`
instead, which reports "-- No entries --" for a scope that is running.

That journal carries the unit's lifecycle, not the build's output - expect a
handful of lines per run, not a live task stream. Build output goes to the run
log (`kas.log`) as before; `bakar log` tails it.

### Journal milestone records

Alongside systemd's own lines, bakar emits a small set of structured records so
a run's shape is queryable next to the scope's resource accounting. Build output
is deliberately excluded: journald rate-limits to 10,000 messages per 30s by
default and drops the excess silently, and a scope unit cannot raise that limit
(`LogRateLimitIntervalSec` is an exec-context property a scope has no way to
set). Set `journal = false` to turn the records off - worth doing on a host whose
journal is already at its `SystemMaxUse` cap and evicting.

| Event | When |
|-------|------|
| `build_start` | Once, with machine, workspace, and host health |
| `progress` | Every `journal_interval` seconds (default 300) |
| `task_failed` | At the failure, not at build end - a `stop_on_error` build keeps draining for minutes afterwards |
| `build_end` | Once, including on an abort or crash |

Every record carries `BAKAR_RUN_ID`, `BAKAR_MACHINE`, `BAKAR_WORKSPACE`, and -
when the build is actually scoped - `BAKAR_SCOPE_UNIT`, which is the join key
back to systemd's `Consumed ... CPU time` and OOM lines for the same unit.

A `progress` record adds build state (`BAKAR_TASKS_DONE`, `BAKAR_TASKS_TOTAL`,
`BAKAR_PCT`, `BAKAR_SSTATE_PCT`, `BAKAR_RUNNING`, `BAKAR_WARNINGS`,
`BAKAR_ERRORS`), host health (`BAKAR_LOAD1`, `BAKAR_PSI_CPU`, `BAKAR_PSI_IO`,
`BAKAR_PSI_MEMORY`, `BAKAR_MEM_AVAIL_MB`, `BAKAR_SWAP_USED_MB`,
`BAKAR_DISK_FREE_GB`), and cache health (`BAKAR_SCCACHE_HIT_PCT`,
`BAKAR_SCCACHE_VERDICT`, `BAKAR_CCACHE_HIT_PCT`). The health fields answer the
question a log file cannot: whether a slow build is saturated or idle and stuck,
and whether its cache quietly stopped hitting.

Because they are real journal fields rather than text, they filter and extract
directly:

```bash
# Follow one run's milestones
journalctl --user BAKAR_RUN_ID=20260807-101500 -f

# Every task failure across all builds this week
journalctl --user BAKAR_EVENT=task_failed --since "1 week ago"

# Progress against host pressure, one line per sample
journalctl --user BAKAR_EVENT=progress --since today \
  -o json --output-fields=BAKAR_PCT,BAKAR_PSI_IO,BAKAR_SCCACHE_HIT_PCT
```

The `progress` snapshot is level-sampled on a fixed tick rather than emitted on
change, so a build that stopped moving shows up as an unchanged record rather
than as silence - which is indistinguishable from a build that finished.

Host vs container mode: an opted-in memory ceiling only bites in host mode,
where kas runs bitbake directly under the scope. In container mode the heavy
work runs inside the `docker`/`podman` container, whose processes live in the
runtime's cgroup - the scope there delivers session-survival but a memory
ceiling only bounds the lightweight `kas-container`/`docker` client.

Tune the limits under `[build] scope*` in `config.toml` (see
[config-reference.md](config-reference.md)); disable per-invocation with the
global `--no-scope` (before the subcommand: `bakar --no-scope build ...`), or
permanently with `bakar settings set build.scope false`. When `systemd-run`
or the user manager is unavailable (e.g. a minimal CI container), bakar warns
once and runs the build unwrapped.

The scope unit name is derived from the workspace + machine, so a second build
of the same target in the same tree deliberately collides with the still-running
scope rather than racing it. One case looked like that collision but was not: a
*finished* `bakar bitbake` run leaves bitbake's memory-resident cooker
(`bitbake-server`) alive by design, and it alone kept the scope `active`, so the
next identical run failed with
`Unit bakar-bitbake-<hash>.scope was already loaded or has a fragment file`
before any bitbake output appeared. bakar now inspects the scope's cgroup before
launching, and when nothing but that idle cooker remains it takes one of two
routes depending on whether this run carries cgroup resource controls:

| Resource controls | What happens | Cost |
|-------------------|--------------|------|
| None (the default) | Launch under a run-scoped unit name, leaving the cooker alive | None - the parse cache carries over |
| `scope_memory_max` set | Stop the unit, then reuse the stable name | The cooker dies and the next run re-parses |

The split exists because a memory ceiling that does not contain the process
doing the allocating is not a ceiling: with controls configured the cooker has
to run inside *this* run's cgroup, and that is worth a re-parse. With no
controls configured the scope's remaining jobs are session-survival, which
applies to the client being launched, and `oom_score_adj`, which the surviving
cooker already inherited at fork and keeps - so stopping it would buy a unit
name and cost a full re-parse of every recipe.

A scope containing a live `kas`/`bitbake` client or any `bitbake-worker` takes
neither route: it keeps the stable name and is left to collide, so a genuinely
concurrent build still fails fast. If the cgroup cannot be read, bakar assumes
the scope is busy and leaves it alone.

Because the launched unit is not always the config-derived one, read the name
bakar logs (or `BAKAR_SCOPE_UNIT` in the journal records) rather than deriving
it yourself.

Interrupting a build with Ctrl-C and immediately re-running it lands in a
related window: the previous build's `kas` client and its parser processes are
still shutting down, so the scope reads as busy even though no build is running
in it. bakar waits up to 15 seconds for such a scope to drain before giving up
(logged as `still draining from a previous run`), which is long enough for the
interrupted processes to exit and short enough that a genuinely concurrent
build - which never drains - still collides as intended. The wait only happens
when a unit of that name is actually loaded, so an ordinary build never pays
for it.

This hardening addresses build **session-survival** (plus host responsiveness,
and opt-in memory containment). It does not prevent filesystem/kernel faults
(e.g. an XFS root-fs corruption panic), which are tracked separately - no cgroup control changes
that class of failure.

## Remote dispatch (`--on <host>`)

`--on <host>` runs the whole build on an idle remote node instead of the local
machine, streams the output back live, and prints the remote run-id so you can
triage failures over ssh. Every other build flag passes through unchanged. When
`--on` is unset the build is byte-identical to a local run: no ssh, git push or
rsync is spawned.

`<host>` is any ssh destination: an alias from `~/.ssh/config` or a `user@ip`.
The remote must run a matching `bakar` version.

Dispatch never deletes anything on the remote. It adds what the build needs and
leaves every existing file, repository and ref in place. How your sources get
there depends on the build:

- A **meta-avocado kas build** (a kas file inside a `meta-avocado` checkout or
  one of its linked worktrees) runs in a release workspace that already exists
  on the remote. Only the checkout holding the kas file travels, as a git commit
  pushed into the remote workspace's own `meta-avocado` clone and checked out
  there as a linked worktree. Nothing is rsynced. See
  [Meta-avocado dispatch](#meta-avocado-dispatch).
- **Every other form** (NXP and TI manifests, bitbake-setup workspaces, qcom,
  and kas files outside `meta-avocado`) mirrors the workspace root to the same
  absolute path on the remote with an additive `rsync -a`. See
  [Additive mirror for other build forms](#additive-mirror-for-other-build-forms).

```bash
# Dispatch a meta-avocado worktree build to pc3; needs [workspaces] template
cd /home/tiamarin/repos/work/peridio
bakar build /home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed/kas/machine/raspberrypi5.yml \
    --target python3-lgpio --on pc3

# Proceed even though pc2 runs a different bakar build
bakar build meta-avocado/kas/machine/qemux86-64.yml --on pc2 --yes

# Force the remote build to join the sccache-dist cluster (off by default)
bakar --sccache-dist build meta-avocado/kas/machine/qemux86-64.yml --on pc2
```

### Release workspaces

A release workspace is a kas workspace for one Yocto release: a `meta-avocado`
clone plus every repository the release's kas files pin, side by side. The
layout is the same on every build host, for example
`/home/tiamarin/repos/work/peridio-wrynose` and
`/home/tiamarin/repos/work/peridio-scarthgap`, so one config maps a release to a
path that is valid locally and on the remote.

bakar reads the release a kas file builds from its pins. It runs
`kas dump --skip repos_checkout --skip finish_setup_repos --skip repos_apply_patches`
on a generated wrapper with a throwaway, empty `KAS_WORK_DIR`, so no existing
repository is touched, and takes the `branch:` of the `openembedded-core` pin
with its `-avocado` suffix removed: `wrynose-avocado` builds `wrynose`,
`scarthgap-avocado` builds `scarthgap`. When the merged config also sets
`env.AVOCADO_YOCTO_SERIES` to a different value, bakar refuses and names both.

The mapping lives in the user-global `~/.config/bakar/config.toml`. Keep it
identical on every host; a remote host needs no config of its own for dispatch:

```toml
[workspaces]
# {release} is the only placeholder
template = "/home/tiamarin/repos/work/peridio-{release}"

[workspaces.releases]
# A per-release entry wins over the template
wrynose = "/home/tiamarin/repos/work/peridio-wrynose"
```

Neither key has a default. A `[workspaces.releases]` entry wins over
`[workspaces] template`, and `~` is expanded in both. A template that uses any
placeholder other than `{release}` is rejected when the config loads, and so is
a `[workspaces.releases]` value that is not a string.

Routing applies to local builds too. Whenever a mapping resolves for the
release, a meta-avocado kas build without `-w` uses the routed workspace as its
workspace and `KAS_WORK_DIR`, wherever the kas file's checkout lives. The
generated wrapper declares `meta-avocado` at the checkout's absolute path, so
kas reads your checkout and not the workspace's own `meta-avocado`. A routed
path that does not exist stops the build with exit 2 before any kas command
runs. With no mapping for the release, a local build resolves its workspace as
before: the parent of the `meta-avocado` main checkout. `-w` always wins.

bakar does not create release workspaces. Create one once per host with the
commands the missing-workspace refusal prints (see
[Refusals and fixes](#refusals-and-fixes)).

### Repository reuse policy

A release workspace is shared by every checkout and worktree that builds that
release. kas moves every clean existing repository to its pin whenever it sets
up repositories, so two worktrees with different pins would keep moving the
shared clones under each other. The reuse policy stops that.

`BAKAR_REPO_POLICY=reuse` is the default whenever routing resolves a workspace,
for local and dispatched builds alike; dispatch always forwards it to the remote
build. Before the kas dump, the build host classifies each pinned repository
that has a `url:`:

| State | Condition | What the build does |
|-------|-----------|---------------------|
| frozen | the checkout exists, sits at its pin and is clean | the overlay sets `url: null`, so kas performs no version-control operation on it |
| missing | no checkout exists | left to kas, which clones it at its pin |
| side | the checkout is on another commit or has local changes | the overlay points the repository's `path:` at `<workspace>/.bakar/pins/<repo>/<commit12>`, which kas clones from the pinned URL; the shared clone is never moved |

The overlay is written to `<build dir>/avocado-reuse.yml` and merged last. Each
new side checkout prints one line,
`side checkout <repo> at <commit12>: <path>`. An existing side checkout that is
clean at its pin is frozen and reused as is; one with local changes or another
HEAD fails the build, naming the path. A repository with a `url:` but no
`commit:` pin also fails the build, because kas would move the shared clone to a
branch tip. Under `reuse` the "does not contain commit" retry with
`--skip repos_checkout` is not attempted, so the original kas error stands.

Set `BAKAR_REPO_POLICY=kas` to opt out and let kas manage the repositories as it
always has, retry included. Any other value is refused, so a typo never hands
the shared clones back to kas. Under `reuse`, a build whose kas file is not inside
a `meta-avocado` checkout prints
`repo policy reuse skipped for <kas file>: not inside a meta-avocado checkout, using the kas policy`
and uses the `kas` policy.

### Worktree build directories

A build whose kas file sits in a linked `meta-avocado` worktree gets its own
build directory, named by `[build] worktree_build_dir`. The default is
`build-{stem}-{worktree}`, where `{stem}` is the kas file stem (for example
`raspberrypi5`) and `{worktree}` is the worktree directory name, so
`rpi5-lgpio-feed` builds in `<workspace>/build-raspberrypi5-rpi5-lgpio-feed`.
`{stem}` and `{worktree}` are the only placeholders. Set the value to
`build-{stem}` to share one directory per machine again. A main-checkout build
keeps `build-{stem}`.

```toml
[build]
worktree_build_dir = "build-{stem}-{worktree}"
```

`BAKAR_WORKTREE_BUILD_DIR` in the environment overrides the config value.
Dispatch forwards the dispatching host's template this way, so the remote build
uses the same name. A value arriving through the environment skips the config
loader, so an unsupported placeholder in it is refused when the build directory
is computed.

Each build directory has its own TOPDIR, so a `local_tmpdir_base` redirect gives
each its own TMPDIR. Builds of the same machine from two worktrees no longer
serialize on one bitbake lock or overwrite each other's generated
`avocado-bakar.yml`.

Worktree builds are tested in host mode only: `kas-container` mounts just the
workspace, so a checkout outside it is not visible inside a container build.

### Meta-avocado dispatch

Dispatch settings, all optional:

```toml
[dispatch]
# Where delivered worktrees go on the remote; default <workspace>/.bakar/worktrees
worktree_root = "/home/tiamarin/repos/work/peridio-wrynose/.bakar/worktrees"
# Refuse a snapshot whose untracked files exceed this many MiB; default 50
snapshot_untracked_cap_mb = 50
```

The dispatcher resolves everything locally and stops at the first failure.
Every check runs before the first remote write:

1. **Release and workspace** - bakar probes the release from the kas pins and
   maps it through `[workspaces]`. With no mapping, `--on` refuses with exit 2
   before any ssh command. The routed path does not have to exist locally; only
   the remote copy matters.
2. **Kas argument** - every colon-separated segment of the kas argument is
   rewritten relative to the checkout, because the remote build runs from the
   delivered worktree. A segment outside the checkout is refused with exit 2.
   `-w`/`--workspace` is dropped from the forwarded command line.
3. **Host preflight** - `ssh -o BatchMode=yes <host> bash -s` runs
   `command -v bakar && bakar --version` over the same non-login bash the build
   uses. An unreachable host, missing key auth, or `bakar` absent from the
   remote non-login PATH fails fast. A local/remote `bakar --version` mismatch
   refuses the dispatch; `--yes` proceeds anyway.
4. **Remote workspace check** - one read-only ssh script checks that the
   workspace exists and that `<workspace>/meta-avocado` is a git repository,
   and reads `<workspace>/openembedded-core/meta/conf/layer.conf`. bakar parses
   `LAYERSERIES_CORENAMES` from that text locally and refuses when it differs
   from the release being built, so a mapping that points at the wrong workspace
   cannot build there.
5. **Commit to deliver** - a clean checkout delivers its HEAD. A dirty one
   (tracked changes, or untracked files that are not ignored) delivers a
   snapshot commit whose parent is HEAD, built through a temporary index file.
   Your branch, index, working tree and stash stack are untouched. The snapshot
   is authored with your git identity, its message is
   `bakar dispatch snapshot of <branch>@<head12>`, and its dates are pinned to
   HEAD's committer date, so dispatching the same dirty tree twice delivers the
   same commit.
6. **Plan** - bakar prints what it is about to add and does not prompt:

   ```text
   dispatch plan -> pc3
     + worktree <workspace>/.bakar/worktrees/<name> (created, or moved to the delivered commit)
     + ref refs/bakar/<name>/<commit12> in <workspace>/meta-avocado
     + build directory <workspace>/build-<stem>-<name>
     nothing is deleted on the host
     snapshot of uncommitted changes: no
   ```

   `<name>` is the worktree directory name, or for a main checkout its current
   branch, with every character outside `[A-Za-z0-9._-]` replaced by `-`. The
   build directory follows `[build] worktree_build_dir`, because the remote
   builds from a linked worktree even when you dispatched from a main checkout.
7. **Push** - `git push` sends the commit to `refs/bakar/<name>/<commit12>` of
   `<workspace>/meta-avocado` on the remote. Each commit gets its own ref; a ref
   is never overwritten and nothing is force-pushed, so every delivered commit
   stays reachable. The receiving side runs as
   `git -c receive.autogc=false -c receive.shallowUpdate=true receive-pack`:
   automatic gc is off because it would prune unreachable objects and stale
   worktree records, and shallow updates are on because a push from a shallow
   clone is otherwise rejected.
8. **Delivered worktree** - a remote script takes a per-name lock on
   `<worktree_root>/.<name>.lock`. When `<worktree_root>/<name>` does not exist
   it runs `git worktree add --detach` for the commit and writes the marker
   `bakar-delivered.json` (source host, source path, name, commit, snapshot
   flag, delivery time) into the worktree's git admin directory, so the work
   tree stays clean. When it exists, bakar moves it to the new commit with
   `git checkout --detach` only if the marker names this host and this checkout
   path, the tree is clean (checked first, so a hand-edited tree at the same
   commit is refused too), and no build holds the `bitbake.lock` of any build
   directory in the workspace, because every machine built from the worktree
   shares it. A worktree already at the commit is left as is.
9. **Remote build** - the build runs from the delivered worktree with the kas
   path relative to it. The remote bakar recognises the linked worktree, takes
   the parent of `<workspace>/meta-avocado` as its workspace and needs no
   config. The environment carries your `BAKAR_*` and `KAS_*` variables except
   `KAS_WORK_DIR`, plus `BAKAR_REPO_POLICY=reuse` and, when a template is
   configured, `BAKAR_WORKTREE_BUILD_DIR`. The remote build prints the side
   checkouts it creates.
10. **Run-id** - the remote run-id is discovered under the remote workspace; see
    [Remote build, streaming and exit codes](#remote-build-streaming-and-exit-codes).

### Additive mirror for other build forms

NXP and TI manifest builds, bitbake-setup workspaces, qcom builds and kas files
outside `meta-avocado` keep the working-tree mirror. The remote must have the
workspace at the same absolute path as the local machine.

1. **Workspace guard** - an empty, relative, filesystem-root or home-directory
   workspace root is refused before anything runs.
2. **Host preflight** - the same preflight and version check as above.
3. **Plan** - bakar prints the plan, one additive transfer of the workspace
   root, with no prompt.
4. **Mirror** - `rsync -a` copies the workspace root to the same absolute path
   on the remote, uncommitted edits included. It adds and updates files and
   never deletes one: a file you removed locally lingers on the remote until you
   remove it by hand, and a stale recipe there can still be parsed. `.git` is
   kept (kas and bitbake read git state for `SRCREV`/`AUTOREV`); build artifacts
   and caches are excluded. Workspace-root outputs are anchored with a leading
   `/` so an unanchored basename cannot also drop a real source directory (for
   example oe-core's `meta/recipes-devtools/ccache/`): `/build/`, `/build-*/`,
   `/*/build/`, `/ccache/`, the package feed `/_feed` and its staging sibling,
   plus the depth-matched `**/tmp/`, `**/sstate-cache/`, `**/downloads/`,
   `**/.venv/`, `**/__pycache__/` and `**/*.pyc`. NFS-mounted shared caches live
   outside the workspace, so they are never in scope.
5. **Remote build** - the build runs from the directory you invoked it from, as
   described below.

### Remote build, streaming and exit codes

1. **sccache-dist off by default** - the remote build runs with
   `BAKAR_SCCACHE_DIST=0`, so a node that is normally an sccache-dist server
   runs as an independent worker (local ccache plus shared sstate) rather than
   re-coupling to the cluster. Pass `--sccache-dist` to opt back in; the
   forwarded flag wins over the env default. The remote command is delivered as
   a script over stdin to `ssh <host> bash -s`, because the remote login shell
   is fish, which mangles a naive `ssh <host> '<cmd>'` or `bash -lc` invocation.
2. **Detach** - the remote build runs under a transient
   `systemd-run --user --unit=bakar-dispatch-<timestamp>` unit, not as a child of
   the ssh session. A child dies with the session: when the local dispatcher was
   killed, sshd sent SIGHUP and took a 50-minute build with it. Under a transient
   unit the build belongs to the remote user manager, so a local Ctrl-C, a
   dropped link or a dead dispatcher costs you the log stream and nothing more.
   The unit receives your session PATH with `--setenv=PATH`, because the user
   manager's PATH lacks `~/.local/bin` and `kas` would not be found. On a remote
   where `systemd-run --user` is unavailable (no systemd, no user runtime dir),
   the script falls back to the old coupled form rather than failing the
   dispatch; the build then still dies with the session.

   Detaching holds only while the remote user manager lives. Without lingering,
   systemd stops that manager when your last session on the host ends, taking
   its transient units with it, so bakar checks and prints a hint when the
   remote user has no linger. Enable it once per build host:
   `ssh <host> loginctl enable-linger`. bakar does not enable it for you: it is a
   persistent change to someone else's machine.
   The unit also carries your ssh session's `SSH_AUTH_SOCK`, proxy and TLS
   trust-store variables, which a transient unit would otherwise not inherit;
   without them a `SRC_URI = "git://...;protocol=ssh"` fetch or a fetch through
   a corporate proxy would fail on the detached path only.
3. **Live streaming and run-id** - the build writes to
   `$XDG_RUNTIME_DIR/<unit>.log` on the remote (0700 and per-user, so another
   local user cannot forge the exit-code sentinel that sits beside it) and your
   terminal tails it. Losing that tail does not touch the build; re-attach with
   `ssh <host> tail -F <the log path bakar printed>`. On completion bakar
   prints the remote run-id and a copy-pasteable
   `ssh <host> bakar triage <run-id>` line so you (or Claude, over ssh) can
   inspect the run remotely.
4. **Exit propagation** - a non-zero remote build exits the local command with
   the same code; the failure message points at the remote triage command. The
   code comes from a sentinel file the remote wrapper writes beside the log, not
   from the ssh session, which exits as soon as the unit starts. Losing the log
   stream exits **75** instead: that is a transport failure, not a build result,
   and the build is still running, so it has to be distinguishable from a build
   that genuinely exited 255.

Stop a detached build with `bakar stop --on <host>` - see [stop.md](stop.md).
Ctrl-C on the dispatching terminal does not reach it: the local command exits
130 and prints the stop and triage commands.

A meta-avocado dispatch opens about seven ssh connections (preflight, workspace
check, the push, the worktree script, the launch, the log follower and the
run-id discovery); the additive mirror opens five (rsync's own ssh in place of
the workspace check, push and worktree script). On a high-latency link, a
`~/.ssh/config` entry with `ControlMaster auto` and a `ControlPersist` window
amortizes them onto one shared connection.

### Refusals and fixes

The routing, preflight, workspace and delivery refusals happen before the first
remote write. The worktree refusals come after the push, so the pushed ref and
its objects stay on the remote; the next dispatch of the same commit reuses
them. The last two refusals come from the build itself, before kas touches the
workspace.

| Message (abridged) | Cause | Fix |
|--------------------|-------|-----|
| `--on for meta-avocado builds needs [workspaces] template in the bakar user config` | no `[workspaces]` table, or no template and no `[workspaces.releases]` entry for this release (exit 2) | add `[workspaces] template` or a `[workspaces.releases]` entry |
| `Cannot route <kas> to a release workspace: the merged kas config has no openembedded-core pin`, `...has no branch:`, `...does not end in '-avocado'` | the release cannot be read from the pins (exit 2) | pin `openembedded-core` with `branch: <release>-avocado` |
| `Cannot route <kas> ...: the openembedded-core pin names release 'x' but AVOCADO_YOCTO_SERIES is 'y'` | the kas tree contradicts itself (exit 2) | fix the pin or the variable |
| `Cannot route <kas> ...: <kas> includes files from repositories other than its own checkout (<repos>)` | a kas include names a repository other than `meta-avocado` (exit 2) | include only files from the checkout |
| `Release workspace <path> for release 'x' does not exist` | local build only: the routed path is missing (exit 2) | create the workspace, fix `[workspaces]`, or pass `-w` |
| `cannot dispatch: kas file '<segment>' is outside the meta-avocado checkout` | a kas segment would not exist in the delivered worktree (exit 2) | keep every kas file and overlay inside the checkout |
| `remote preflight failed for <host>` | host unreachable, no key auth, or no `bakar` on the remote non-login PATH | fix ssh or install bakar on the remote |
| `bakar mismatch: local '...' vs remote '...'` | different bakar builds on the two hosts | sync the remote bakar, or pass `--yes` |
| `remote workspace check failed: the release workspace <ws> does not exist on <host>` | no workspace on the remote | run the printed one-time commands on the host: `mkdir -p <ws>`, `git clone --branch <release> <meta-avocado origin URL> <ws>/meta-avocado`, `env KAS_WORK_DIR=<ws> kas checkout <ws>/meta-avocado/kas/machine/<machine>.yml` |
| `remote workspace check failed: <ws>/meta-avocado on <host> is missing or is not a git repository` | the workspace has no `meta-avocado` clone | clone it there at the release branch |
| `remote workspace check failed: <ws>/openembedded-core/meta/conf/layer.conf on <host> is missing or unreadable`, or `...declares no LAYERSERIES_CORENAMES` | the workspace was never checked out | run `kas checkout` in that workspace |
| `remote workspace check failed: release mismatch: the build needs 'x' but <ws> on <host> declares 'y'` | the mapping points at another release's workspace | fix `[workspaces]` |
| `cannot deliver <checkout>: untracked files in <checkout> total N MiB, above the <cap> MiB snapshot cap` | large untracked files would ride in the snapshot | commit, ignore or remove them, or raise `[dispatch] snapshot_untracked_cap_mb` |
| `cannot dispatch: the name '<raw>' has no usable characters after sanitising` | the worktree or branch name has no character in `[A-Za-z0-9._-]` (exit 2) | rename the worktree or branch |
| `cannot deliver <checkout>: <checkout> has a detached HEAD` | a main checkout on no branch has no name to deliver under | check out a branch, or build from a linked worktree |
| `BAKAR_REPO_POLICY='x' is not a repository policy` | the variable holds a value other than `kas` or `reuse`; the build and the doctor refuse it rather than fall back to `kas` | unset it, or set `kas` or `reuse` |
| `git push failed (exit N); remote build not started.` | git's own message is printed above it | act on git's message |
| `could not place the worktree <path> on <host>: another dispatch of the same name holds the delivery lock` | a dispatch of the same name is still delivering (script exit 3) | wait for it |
| `...: the delivery path exists but carries no readable bakar marker` | bakar did not create `<worktree_root>/<name>` (script exit 4) | move that directory aside by hand, or dispatch from a worktree with another name |
| `...: the delivered worktree belongs to another host or checkout` | the marker names another dispatching host or checkout path (script exit 5) | dispatch under another name, or remove that worktree by hand |
| `...: the delivered worktree has uncommitted changes` | the delivered tree was edited on the remote; the changed paths are listed (script exit 6) | inspect and clean it by hand; bakar never discards changes |
| `--clean cannot be combined with --on` | `--clean` would delete a build directory on the remote (exit 2) | build with `--on`, or run `bakar build --clean` on that host |
| `...: a build is running in the delivered worktree's build directory` | a `bitbake.lock` in any build directory of the workspace is held (script exit 7) | wait for the build, or `bakar stop --on <host>` |
| `repository 'x' has a url but no commit pin; kas would move the shared clone to a branch tip` | the build runs under `reuse` | pin the repository to a commit |
| `side checkout <path> for 'x' is not clean at pin <commit>` | an existing side checkout was changed | inspect it by hand; bakar never modifies it |

### Manual cleanup

Dispatch only adds. Delivered worktrees, `refs/bakar/*` refs and their objects,
side checkouts under `<workspace>/.bakar/pins` and per-worktree build
directories accumulate on each remote, and bakar never removes them. Clean up by
hand on the host. First make sure nothing is building there: no
`bakar-dispatch-*` unit is active and no `bitbake.lock` you are about to remove
is held.

```bash
ws=/home/tiamarin/repos/work/peridio-wrynose
name=rpi5-lgpio-feed

# Is anything building?
systemctl --user list-units 'bakar-dispatch-*.service'
flock -n -s "$ws/build-raspberrypi5-$name/build/bitbake.lock" true && echo idle

# What dispatch has added
git -C "$ws/meta-avocado" worktree list
git -C "$ws/meta-avocado" for-each-ref refs/bakar/
ls "$ws/.bakar/worktrees" "$ws/.bakar/pins"

# A delivered worktree (git refuses if it has changes) and its lock file
git -C "$ws/meta-avocado" worktree remove "$ws/.bakar/worktrees/$name"
rm "$ws/.bakar/worktrees/.$name.lock"

# Its refs, one per delivered commit
git -C "$ws/meta-avocado" for-each-ref --format='%(refname)' "refs/bakar/$name/" |
    xargs -r -n1 git -C "$ws/meta-avocado" update-ref -d

# Its build directory
rm -rf "$ws/build-raspberrypi5-$name"

# A side checkout, once no build directory's avocado-reuse.yml names it
grep -l '.bakar/pins/meta-raspberrypi/6295d0b1d51d' "$ws"/build-*/avocado-reuse.yml
rm -rf "$ws/.bakar/pins/meta-raspberrypi/6295d0b1d51d"
```

Remove a worktree before its refs: the worktree's HEAD also keeps its commit
reachable. Objects of deleted refs stay until you run `git gc` in
`$ws/meta-avocado` yourself, because dispatch disables automatic gc on push.

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
- [bitbake.md](bitbake.md#cache-mount-gate) - cache mount gate detail and the live-console failure-detail forwarding
