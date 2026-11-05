# bakar init

Scaffold a new bakar workspace and choose its features. The interactive wizard walks through the BSP family, workspace directory, and family-specific defaults, writes `.bakar.toml` (and the family subdirectory for nxp/ti), then runs a feature step over the [feature catalog](features/index.md) (caches, mirrors, dispatch and the rest) and optionally kicks off `bakar sync`. Passing `--family` alone runs the same scaffolding non-interactively from flags + built-in defaults (no TTY, no sync) for CI and scripts; `--feature` and `--disable` run the feature step non-interactively.

This is the documented "start here" entry point: you no longer need to know the `mkdir nxp/` / `touch .bakar.toml` conventions up front. The selections you make are persisted into `.bakar.toml` as workspace-scoped defaults so you don't have to re-pass them as flags on every invocation.

## Synopsis

```text
bakar init [OPTIONS]
```

`init` runs one of three ways, chosen by which flags you pass:

- No `--family`, `--feature` or `--disable`: the interactive wizard, collecting every input through prompts (requires a TTY).
- `--family` alone: the scaffold-only path. No TTY, no feature step, no host change; values come from flags and the family's built-in defaults.
- `--feature` or `--disable`, with or without `--family`: the non-interactive feature step, described under [Non-interactive feature step](#non-interactive-feature-step).

`--explain` is a read-only form that prints one feature and exits.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--family` | `-f` | BSP family (`nxp`/`ti`/`bbsetup`/`generic`). Enables non-interactive mode |
| `--workspace` | `-w` | Workspace directory (default: current directory) |
| `--manifest` | | Manifest filename (nxp/ti only) |
| `--machine` | | Machine name |
| `--distro` | | Distro (nxp/ti only) |
| `--image` | | Image (nxp/ti only) |
| `--kas-yaml` | | KAS YAML filename (generic only) |
| `--no-sync` | | Skip the post-scaffold sync prompt (interactive mode only) |
| `--feature` | | Feature id to enable; repeatable. Enables non-interactive mode |
| `--disable` | | Feature id to turn off; repeatable. Enables non-interactive mode |
| `--set` | | `KEY=VALUE` for a selected feature; repeatable. The key is a settings key or a `<feature-id>.<input>` input |
| `--yes` | `-y` | Apply a plan that needs sudo without prompting (needs passwordless sudo) |
| `--explain` | | Explain one feature id and exit; read-only |

With `--family` alone, any value not passed as a flag is read from the family's
built-in defaults (the same defaults the interactive prompts pre-fill), and sync
is never run.

## Prompts

`init` asks for the workspace directory, then (for a new workspace) the BSP family and a family-specific set, then the feature step, then a sync prompt. The sync prompt appears only when this run scaffolded the workspace.

1. **Workspace directory** - a path prompt, default `.` (the current directory). Skipped when you pass `-w`.
2. **BSP family** - a select prompt offering `nxp`, `ti`, `bbsetup`, `generic`. Asked only when the directory has no `.bakar.toml` yet.

When the directory already holds a `.bakar.toml`, the interactive wizard prints `workspace already initialized at <path>` and continues straight to the feature step instead of failing. Earlier releases treated an initialized workspace as an error here; `--family` alone still does.

The family-specific prompts that follow depend on your family choice.

### nxp

Prompts for `manifest`, `machine`, `distro`, `image`. Each default is read at call time from the NXP `BspModel` (the same built-in defaults `bakar build` uses), so accepting every prompt gives you a working NXP config.

Scaffolds an `nxp/` subdirectory and writes a `[defaults.nxp]` section to `.bakar.toml`:

```toml
# bakar workspace root.

[defaults.nxp]
manifest = "imx-6.6.52-2.2.2.xml"
machine  = "imx8mp-var-dart"
distro   = "fsl-imx-xwayland"
image    = "core-image-minimal"
```

### ti

Prompts for `manifest`, `machine`, `distro`, `image`, with defaults read from the TI `BspModel`. Scaffolds a `ti/` subdirectory and writes a `[defaults.ti]` section to `.bakar.toml`.

### bbsetup

No family-specific prompts. bitbake-setup workspaces are driven by `bitbake-setup init`, which is its own interactive wizard with its own state (`config/config-upstream.json`); bakar does not wrap it. `init` writes a comment-only `.bakar.toml` marker so the directory is detected as a workspace root, then points you at the follow-up step.

### generic

Prompts for `kas_yaml` (default `kas-generic.yml`) and `machine` (default `qemux86-64`). Writes a `[defaults.generic]` section to `.bakar.toml` and creates no subdirectory - your kas YAML lives at the workspace root.

```toml
# bakar workspace root.

[defaults.generic]
kas_yaml = "avocado-bspctl.yml"
machine  = "qemux86-64"
```

## Feature step

Once the workspace exists, the interactive wizard offers the [feature catalog](features/index.md) as one checklist.

1. **Checklist pre-filled from config.** Features your user config already enables start checked. A feature whose hard requirement is not met is shown disabled with the reason beside it. An already-enabled feature that is shown disabled stays selected and is never planned for removal here.
2. **Value prompts.** Each newly selected feature asks for the settings and inputs it needs, such as an endpoint URL or a bind address. A required value cannot be left empty. If a feature's requirement fails once the values are known, `init` prints the reason, the fix and the guide page, and you choose to re-enter the values or drop the feature.
3. **One plan.** The plan combines the config settings the features change with the host remediations `bakar setup` would apply (sysctl, docker, and so on), so the same actions run whether you start from `init` or `setup`. Root operations print as one auditable script.
4. **One confirmation.** `Apply this plan?` defaults to no. Nothing is written to `~/.config/bakar/config.toml` before you confirm. Declining prints `Nothing was changed.` A plan with nothing to change prints `Nothing to apply.`
5. **Replay line.** After applying, `init` prints `To repeat this without prompts:` followed by a `bakar init -w <workspace> --feature ... --disable ... --set ...` command that reproduces your choices.

URL passwords never appear in output. Everything printed (the plan, the privileged script, warnings, the replay line) shows `http://***@host/path` in place of `http://user:password@host/path`.

## Non-interactive feature step

Passing `--feature` or `--disable` runs the feature step without prompts or a TTY.

```bash
# Enable two features on an initialized workspace
bakar init -w ~/bsp/imx --feature ccache --feature hashserv

# Scaffold and enable in one call, supplying a value
bakar init -f generic -w ~/bsp/ci --feature dl-mirror \
  --set build.dl_mirror_url=http://mirror.example.com:18742/downloads

# Turn a feature off
bakar init -w ~/bsp/imx --disable ccache
```

Rules:

- **Trigger.** `--family`, `--feature` or `--disable` selects non-interactive mode. `--family` alone keeps the scaffold-only path and its `workspace already initialized` error. Once `--feature` or `--disable` is present, the feature step runs instead.
- **Workspace.** With `--family` and no `.bakar.toml`, the workspace is scaffolded first. Without `--family` the workspace must already exist, otherwise `init` exits 1 with `no workspace here`.
- **Selection.** The starting set is what your user config already enables, plus every `--feature`, minus every `--disable`. An unknown feature id exits 1 and lists the valid ids.
- **`--set KEY=VALUE`.** The key is a settings key (for example `build.dl_mirror_url`) or a feature input written `<feature-id>.<input>` (for example `central-node.bind_host`). A key that belongs to no selected feature is an error, and so is a missing required value; the message names the `--set` to pass.
- **Requirements.** An unmet hard requirement of a feature you are enabling exits 1 with the reason, the fix and the guide. Soft requirements print as warnings.
- **Root.** A plan that needs root exits 1 and lists the privileged steps unless you pass `--yes`. `--yes` needs passwordless sudo.
- **Still in effect.** After a `--disable`, `init` prints a note when the setting is still set by the workspace `.bakar.toml` or a `BAKAR_*` environment variable, because it leaves both alone.

## Explain a feature

```bash
bakar init --explain remote-dispatch
```

Prints the feature's title and summary, each requirement as `met`, `unmet`, `not checked` or `not applicable` (marked required or recommended), the fix for every unmet one, a `checks: N/M run` line and the guide page. It is read-only: nothing is written, no plan is applied and no workspace is created.

`--explain` takes one feature id and combines only with `-w`; any other option exits 1. An unknown id exits 1 and lists the valid ids.

## Sync prompt

After the feature step, and only when this run scaffolded the workspace, `init` prints the warning on its own line:

```text
Downloading sources can take a while
```

Then asks "Run `bakar sync` now?", defaulting to **no**. NXP and TI source syncs each pull multiple gigabytes, so the warning is shown before the prompt rather than after you answer yes.

- **Yes** - `init` calls the existing sync pipeline in-process against the scaffolded workspace, then offers the buildtools install (see below).
- **No** - `init` prints the manual next step. For nxp/ti/generic that is `bakar sync --workspace <path>`; for bbsetup it is `bitbake-setup init` run from inside the workspace. In host mode, when no buildtools toolchain is present, it also prints a hint to run `bakar setup` from the workspace after `bakar sync`.

## Buildtools step

Host builds need the `buildtools-extended` toolchain, which comes from oe-core and so exists only after sync. After a sync that `init` started, in host mode, `init` lists the buildtools install actions and asks `Install the buildtools toolchain?` (default no). Only the `host-preflight` actions are offered; the feature step already handled the rest of the host plan.

Declining returns without installing and prints no hint of its own, so run `bakar setup` later (see [setup.md](setup.md)). The step does not appear when the workspace already existed, when you skip sync, or in container mode.

## Non-TTY failure mode

The **interactive** wizard requires a terminal. Before any prompt it checks `sys.stdin.isatty()`; when stdin is not a TTY (a pipe, CI runner, or redirected input) it prints a message and exits non-zero:

```text
bakar init requires an interactive terminal - stdin is not a TTY.
Use --family or --feature to enable non-interactive mode.
```

For scriptable workspace creation, pass `--family` (with the relevant `--manifest`/`--machine`/`--distro`/`--image`/`--kas-yaml` flags, or accept the built-in defaults). That path needs no TTY and never runs sync. For scripted feature selection, see [Non-interactive feature step](#non-interactive-feature-step).

Pressing Ctrl+C at any interactive prompt aborts (exit 1). Before the feature step nothing has been written. Once a new workspace has been scaffolded, an abort leaves that scaffold in place and writes no user config; run `bakar init` again to continue with the feature step. That re-run does not offer the sync step again.

## Known limits

- A non-interactive run with `--family` scaffolds the workspace before it verifies the requirements, so a refused run leaves the `.bakar.toml` it created. Check a feature's requirements first with `bakar init --explain <feature>`.
- Whether a host is a central node is read from whether the central systemd units are enabled. After you switch a host from central-node to central-client, the units stay enabled until you disable them, so the next `bakar init` still lists central-node as enabled. See [central-tier](features/central-tier.md) for the disable command.
- The release-workspace probe reads the template from your current config, so an unset of `workspaces.template` in the same run is not reflected in its result.

## Examples

### NXP Variscite-style workspace

```bash
mkdir ~/bsp/imx8mp && cd ~/bsp/imx8mp
bakar init
# family:    nxp
# directory: .
# manifest:  imx-6.6.52-2.2.2.xml   (accept default)
# machine:   imx8mp-var-dart        (accept default)
# distro:    fsl-imx-xwayland       (accept default)
# image:     core-image-minimal     (accept default)
# features:  pick from the checklist, confirm the plan
# Run `bakar sync` now? N
```

Result: an `nxp/` subdirectory and a `.bakar.toml` carrying `[defaults.nxp]`. From then on, `bakar build` and `bakar sync` run from this directory pick up the manifest/machine/distro/image without flags.

### Generic Peridio-style workspace

```bash
cd ~/repos/peridio
bakar init
# family:    generic
# directory: .
# kas YAML filename: avocado-bspctl.yml
# machine:           qemux86-64
# features:          pick from the checklist, confirm the plan
# Run `bakar sync` now? N
```

Result: a `.bakar.toml` at the repo root carrying `[defaults.generic]` with `kas_yaml = "avocado-bspctl.yml"` and `machine = "qemux86-64"`. No subdirectory is created - the kas YAML lives at the workspace root.

### bitbake-setup workspace

```bash
mkdir ~/bsp/upstream && cd ~/bsp/upstream
bakar init
# family:    bbsetup
# directory: .
# features:  pick from the checklist, confirm the plan
# Run `bakar sync` now? N
# Next: run `bitbake-setup init` from inside the workspace
#       to populate config/config-upstream.json
```

Result: a comment-only `.bakar.toml` marker. `init` does not prompt for a manifest because `bitbake-setup init` drives its own setup. Run that command next, from inside the workspace, before `bakar build`.

### Non-interactive (CI / scripts)

```bash
# Generic workspace from flags - no TTY, no prompts, no sync
bakar init --family generic --kas-yaml avocado-bspctl.yml --machine qemux86-64 -w ~/bsp/ci

# NXP workspace accepting the built-in manifest/machine/distro/image defaults
bakar init --family nxp -w ~/bsp/nxp
```

`--family`, `--feature` or `--disable` switches off the prompts; omit all three and `init` runs the wizard.

## See also

- [features/index.md](features/index.md) - every feature the step offers, with what each needs from your host
- [setup.md](setup.md) - the host remediations the plan shares with `bakar setup`
- [workspace.md](workspace.md) - workspace detection and the `.bakar.toml` defaults schema
- [configuration.md](configuration.md) - the full config precedence chain including the workspace tier
- [sync.md](sync.md) - sync sources standalone (the step `init` optionally kicks off)
- [build.md](build.md) - full build pipeline
