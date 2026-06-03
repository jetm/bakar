# bakar inspect

Print a deep per-recipe inspection report by combining three bitbake calls inside kas-container.

## Synopsis

```text
bakar inspect <recipe> [OPTIONS]
```

## Description

`bakar inspect` aggregates the output of three (or four with `--recursive`) bitbake calls
into one structured report covering everything bakar knows about a recipe before it builds.

| Section | Source | Content |
|---------|--------|---------|
| **Identity** | `bitbake-layers show-recipes -f`, `bitbake -e` | PN, PV, PR, providing layer, recipe file path, applied `.bbappend` files |
| **Sources** | `bitbake -e` | SRC_URI, LICENSE, LIC_FILES_CHKSUM |
| **Paths** | `bitbake-getvar -r <recipe> WORKDIR S B D T` | Resolved WORKDIR, S (source dir), B (build dir), D (staging dir), T (temp dir) |
| **Inherits** | `bitbake -e` | bbclasses inherited by the recipe (from INHERITED) |
| **Packages** | `bitbake -e` | PACKAGES list with per-package RDEPENDS |
| **Dependencies** | `bitbake -e` | Build deps (DEPENDS) and runtime deps (RDEPENDS) |

With `--recursive/-r` the Dependencies section gains two additional subsections: transitive
forward deps (the full build graph from `bitbake -g`) and transitive reverse deps.

For an unknown recipe, `inspect` exits non-zero and surfaces the bitbake error rather than
printing an empty report as success.

## kas-container requirement

All data comes from bitbake running inside kas-container. A synced workspace with a working
container image is required. Run `bakar sync` first if the workspace has not been initialized.

The bitbake calls issued are:

1. `bitbake-layers show-recipes -f <recipe>` - layer, recipe file, bbappends
2. `bitbake-getvar -r <recipe> WORKDIR S B D T` - resolved build paths
3. `bitbake -e <recipe>` - full environment dump (Sources, Inherits, Packages, Dependencies)
4. `bitbake -g <recipe>` - transitive dep graph (`--recursive` only)

Run logs for each step are written to `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/` as
`inspect-show-recipes.log`, `inspect-getvar-paths.log`, `inspect-env.log`, and
(when `--recursive`) `inspect-recursive.log`. Use `bakar log` to inspect them.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit the report as a single JSON document |
| `--recursive` | `-r` | Add transitive forward and reverse dependencies to the Dependencies section |

## --recursive note

`--recursive` triggers `bitbake -g <recipe>`, which writes `pn-buildlist` and
`task-depends.dot` to the build directory and emits the dependency graph to stdout.
The forward list comes from the captured stdout; reverse deps from `bitbake -g` are
sparse in the current implementation (the reverse list is empty unless bitbake emits
them directly, which it does not in most versions). Full reverse dep resolution
(`bitbake-layers show-recipes --filter-by-provides`) is not yet implemented.

## JSON output

`--json` emits a single JSON document. Top-level keys:

```text
identity      object   PN, PV, PR, layer, recipe_file, bbappends
sources       object   SRC_URI, LICENSE, LIC_FILES_CHKSUM
paths         object   WORKDIR, S, B, D, T
inherits      array    bbclass names
packages      array    {package, rdepends} per package
dependencies  object   DEPENDS (array), RDEPENDS (array);
                       with --recursive: transitive_forward (array), transitive_reverse (array)
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Report printed successfully |
| 2 | No workspace found from the current directory and no `--workspace` given |
| other | bitbake exited non-zero (unknown recipe, parse error, or container failure) |

## Examples

```bash
# Inspect busybox in an NXP workspace
bakar inspect busybox -f imx-6.12.49-2.2.0.xml

# Include transitive dependencies
bakar inspect core-image-minimal -f imx-6.12.49-2.2.0.xml --recursive

# JSON output for scripting
bakar inspect busybox -f imx-6.12.49-2.2.0.xml --json | jq .identity

# Machine override
bakar inspect linux-imx -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# BYO kas YAML workspace - run from the workspace directory
bakar inspect busybox

# Explicit workspace root from an unrelated directory
bakar inspect busybox -f imx-6.12.49-2.2.0.xml -w /path/to/workspace
```

## See also

- [getvar.md](getvar.md) - resolve a single variable with full assignment history
- [layers.md](layers.md) - per-layer detail and project-level status via bitbake
- [shell.md](shell.md) - drop into the container to run bitbake tooling directly
- [log.md](log.md) - tail the inspect run logs
- [sync.md](sync.md) - sync sources before running container-backed commands
