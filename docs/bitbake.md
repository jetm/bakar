# bakar bitbake

Run a single recipe or image target through bitbake inside kas-container, with the run logged.

## Synopsis

```text
bakar bitbake <target> [kas_yaml] [OPTIONS]
bakar clean-recipe <recipe> [kas_yaml] [OPTIONS]
```

## Description

`bakar bitbake` is a recipe-level passthrough to bitbake running inside kas-container.
By default it issues `bitbake <target>`; with `--task/-c` it issues `bitbake -c <task> <target>`.
Unlike `bakar shell -c "bitbake <recipe>"`, every invocation is logged to the per-run dir and
exits with bitbake's own exit code, surfacing a non-zero result rather than reporting success.

`bakar clean-recipe` is a thin alias for `bitbake -c cleansstate <recipe>` covering the most
common cleanup task. It shares the same task-execution path, logging, and exit-code behavior.

Two task names are special-cased:

| `--task` value | Behavior |
|----------------|----------|
| `listtasks` | Runs `bitbake -c listtasks <target>`, captures the output, and pretty-prints the parsed `do_*` task names |
| `devshell` | Routes through the interactive path (TTY attached); output is not captured to a log |

Every other invocation captures bitbake's output to a log file under the run dir.

## Workspace dispatch

The BSP family is resolved from how you point bakar at the workspace:

- **BYO / bbsetup**: pass the positional `kas_yaml` (e.g. `meta-avocado/kas/machine/qemux86-64.yml`);
  the workspace is resolved next to it.
- **NXP / TI**: pass `-f/--manifest` (NXP `.xml` or TI `.txt`); the family is dispatched from the
  manifest filename.

Run from inside a workspace and both can be omitted.

## kas-container requirement

bitbake runs inside kas-container, so a synced workspace with a working container image is
required. Run `bakar sync` first if the workspace has not been initialized.

## Run logging

Each non-interactive invocation writes its captured output to
`<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/` as `bitbake.log` (for `bakar bitbake`) or
`clean-recipe.log` (for `bakar clean-recipe`). Use `bakar log` to inspect them. The `devshell`
path is interactive and produces no captured log.

## Options

### `bakar bitbake`

| Flag | Short | Description |
|------|-------|-------------|
| `--task` | `-c` | bitbake task to run (e.g. `compile`, `listtasks`, `devshell`); omit to run the default build |
| `--keep-going` | `-k` | Pass `-k` to bitbake (keep building after failures) |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |

### `bakar clean-recipe`

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |

`clean-recipe` has no `--task` or `--keep-going`; its task is fixed to `cleansstate`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | bitbake completed successfully |
| 2 | No workspace found from the current directory and no `--workspace` given |
| other | bitbake exited non-zero (propagated verbatim) |

## Examples

```bash
# Build busybox in a BYO/bbsetup workspace
bakar bitbake busybox meta-avocado/kas/machine/qemux86-64.yml

# Run only the compile task
bakar bitbake busybox --task compile meta-avocado/kas/machine/qemux86-64.yml

# List the available tasks for a recipe
bakar bitbake busybox --task listtasks meta-avocado/kas/machine/qemux86-64.yml

# Drop into an interactive devshell
bakar bitbake busybox --task devshell meta-avocado/kas/machine/qemux86-64.yml

# Keep building after a failure
bakar bitbake core-image-minimal --keep-going meta-avocado/kas/machine/qemux86-64.yml

# NXP workspace via manifest dispatch
bakar bitbake busybox -f imx-6.12.49-2.2.0.xml

# Clean a recipe's sstate
bakar clean-recipe busybox meta-avocado/kas/machine/qemux86-64.yml
```

## See also

- [inspect.md](inspect.md) - deep per-recipe inspection report before building
- [graph.md](graph.md) - dependency-graph analysis from `bitbake -g` output
- [shell.md](shell.md) - drop into the container to run bitbake tooling directly
- [log.md](log.md) - tail the run logs
- [sync.md](sync.md) - sync sources before running container-backed commands
