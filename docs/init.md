# bakar init

Interactively scaffold a new bakar workspace. Walks through the BSP family, workspace directory, and family-specific defaults, writes `.bakar.toml` (and the family subdirectory for nxp/ti), then optionally kicks off `bakar sync`.

This is the documented "start here" entry point: you no longer need to know the `mkdir nxp/` / `touch .bakar.toml` conventions up front. The selections you make are persisted into `.bakar.toml` as workspace-scoped defaults so you don't have to re-pass them as flags on every invocation.

## Synopsis

```text
bakar init
```

`init` takes no flags. Every input is collected through interactive prompts.

## Prompts

`init` always asks two questions first, then a family-specific set, then a final sync prompt.

1. **BSP family** - a select prompt offering `nxp`, `ti`, `bbsetup`, `generic`.
2. **Workspace directory** - a path prompt, default `.` (the current directory).

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

## Sync prompt

After scaffolding, `init` prints the warning on its own line:

```text
Downloading sources can take a while
```

Then asks "Run `bakar sync` now?", defaulting to **no**. NXP and TI source syncs each pull multiple gigabytes, so the warning is shown before the prompt rather than after you answer yes.

- **Yes** - `init` calls the existing sync pipeline in-process against the scaffolded workspace.
- **No** - `init` prints the manual next step. For nxp/ti/generic that is `bakar sync --workspace <path>`; for bbsetup it is `bitbake-setup init` run from inside the workspace.

## Non-TTY failure mode

`init` requires an interactive terminal. Before any prompt it checks `sys.stdin.isatty()`; when stdin is not a TTY (a pipe, CI runner, or redirected input) it prints a message naming the requirement and exits non-zero:

```text
bakar init requires an interactive terminal - stdin is not a TTY.
Create the workspace manually with `mkdir <family>/ && touch .bakar.toml`.
```

There is no `--non-interactive` flag. For scriptable workspace creation, create the layout by hand: `mkdir nxp/ && touch .bakar.toml` (or the `ti/` equivalent), or `touch .bakar.toml` for a generic workspace.

Pressing Ctrl+C at any prompt aborts cleanly (exit 1) without writing a partial workspace.

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
# Run `bakar sync` now? N
```

Result: a `.bakar.toml` at the repo root carrying `[defaults.generic]` with `kas_yaml = "avocado-bspctl.yml"` and `machine = "qemux86-64"`. No subdirectory is created - the kas YAML lives at the workspace root.

### bitbake-setup workspace

```bash
mkdir ~/bsp/upstream && cd ~/bsp/upstream
bakar init
# family:    bbsetup
# directory: .
# Run `bakar sync` now? N
# Next: run `bitbake-setup init` from inside the workspace
#       to populate config/config-upstream.json
```

Result: a comment-only `.bakar.toml` marker. `init` does not prompt for a manifest because `bitbake-setup init` drives its own setup. Run that command next, from inside the workspace, before `bakar build`.

## See also

- [workspace.md](workspace.md) - workspace detection and the `.bakar.toml` defaults schema
- [configuration.md](configuration.md) - the full config precedence chain including the workspace tier
- [sync.md](sync.md) - sync sources standalone (the step `init` optionally kicks off)
- [build.md](build.md) - full build pipeline
