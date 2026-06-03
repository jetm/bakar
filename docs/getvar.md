# bakar getvar

Resolve a BitBake variable inside kas-container and show where it was set.

## Synopsis

```text
bakar getvar <VAR> [OPTIONS]
```

## Description

`bakar getvar` runs `bitbake-getvar` (or `bitbake -e` with `--history`) inside
kas-container and prints the resolved value of a single BitBake variable.

Two modes of resolution are available:

| Mode | What it runs |
|------|-------------|
| **Global** (no `--recipe`) | `bitbake-getvar <VAR>` - the value as it resolves in the global configuration context |
| **Recipe-scoped** (`--recipe <name>`) | `bitbake-getvar -r <name> <VAR>` - the value after recipe-specific overrides and appends |

A single kas-container invocation is needed for each call. The workspace must
be synced and the container image must be available.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--recipe` | `-r` | Resolve the variable within this recipe's parse context |
| `--unexpanded` | `-u` | Print the value before `${...}` expansion |
| `--history` | | Show the ordered list of files and lines where the variable was set across the include chain |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit a JSON document instead of formatted text |

## Modes

### Global resolution

Without `--recipe`, `bitbake-getvar` evaluates the variable in the global
configuration context (the same environment that `local.conf`, `site.conf`, and
the layer `conf/layer.conf` files build up):

```bash
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml
```

### Recipe-scoped resolution

With `--recipe`, bitbake parses the recipe first and evaluates the variable in
that recipe's context. Useful for variables that recipes override or append to
(`IMAGE_INSTALL`, `DEPENDS`, `SRC_URI`):

```bash
bakar getvar IMAGE_INSTALL -f imx-6.12.49-2.2.0.xml --recipe core-image-minimal
```

### Unexpanded value

`--unexpanded` passes the `-e` flag to `bitbake-getvar`, which prints the value
before `${...}` references are substituted. This is helpful when you want to see
the literal assignment rather than the fully resolved string:

```bash
bakar getvar WORKDIR -f imx-6.12.49-2.2.0.xml --unexpanded
# prints something like: ${TMPDIR}/work/${MULTIMACH_TARGET_SYS}/${PN}/${EXTENDPE}${PV}-${PR}
```

### History (include-chain provenance)

`--history` runs `bitbake -e` (or `bitbake -e <recipe>` when `--recipe` is also
given), feeds the full environment dump to the `extract_var_history` parser, and
prints the ordered list of `file:line` source locations where the variable was
set or appended - earliest assignment first, final override last:

```bash
bakar getvar BB_NUMBER_THREADS -f imx-6.12.49-2.2.0.xml --history
```

Example output:

```text
BB_NUMBER_THREADS history (include-chain order):
  /layers/poky/meta/conf/bitbake.conf:100
  /builds/conf/local.conf:14
```

When no history comments are present in the environment dump (the variable is
set by a method or internal bitbake mechanism that does not emit history
comments), `getvar` prints `no history recorded` and exits 0. An empty history
is not an error.

## JSON output

`--json` emits a single JSON document. The shape depends on the mode:

**Without `--history`:**

```text
var     string  variable name
value   string  resolved value
recipe  string  recipe name (present only when --recipe was given)
```

**With `--history`:**

```text
var      string        variable name
history  array[string] ordered file:line source locations (empty array when no history)
recipe   string        recipe name (present only when --recipe was given)
```

## kas-container requirement

`getvar` always runs inside kas-container. The workspace must be synced
(`bakar sync`) and the container image must be available. `--history` additionally
requires bitbake to parse the full recipe environment, which can take longer
than a plain `bitbake-getvar` call.

`getvar` is read-only: it does not modify the build directory, sstate, or any
workspace files.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Value (or history) printed; `--history` with no history comments exits 0 with "no history recorded" |
| 2 | No workspace found from the current directory and no `--workspace` given |
| other | Underlying `bitbake-getvar` or `bitbake -e` call failed; exit code is forwarded |

## Examples

```bash
# Resolve MACHINE globally
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml

# Resolve IMAGE_INSTALL within core-image-minimal's parse context
bakar getvar IMAGE_INSTALL -f imx-6.12.49-2.2.0.xml --recipe core-image-minimal

# Show the unexpanded (pre-substitution) value of WORKDIR for a recipe
bakar getvar WORKDIR -f imx-6.12.49-2.2.0.xml --recipe busybox --unexpanded

# Show where BB_NUMBER_THREADS was set across the include chain
bakar getvar BB_NUMBER_THREADS -f imx-6.12.49-2.2.0.xml --history

# Recipe-scoped history for IMAGE_INSTALL
bakar getvar IMAGE_INSTALL -f imx-6.12.49-2.2.0.xml --recipe core-image-minimal --history

# JSON output for scripting
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml --json | jq .value

# History as JSON (empty array means no history recorded)
bakar getvar BB_NUMBER_THREADS -f imx-6.12.49-2.2.0.xml --history --json

# Machine override for a multi-machine workspace
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Explicit workspace root
bakar getvar DISTRO -f imx-6.12.49-2.2.0.xml -w /srv/bsp/nxp
```

## See also

- [show.md](show.md) - local-only resolved-config report (no container needed)
- [inspect.md](inspect.md) - full per-recipe report including paths, deps, and inherits
- [layers.md](layers.md) - per-layer detail and project-level variable summary via `layers status`
- [shell.md](shell.md) - drop into the container to run `bitbake-getvar` manually
