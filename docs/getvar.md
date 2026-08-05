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
| `--flag` | | Resolve a variable flag instead of the variable itself - equivalent to `VAR[FLAG]` |
| `--history` | | Show the ordered list of files and lines where the variable was set across the include chain |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit a JSON document instead of formatted text |

`--flag` is long-only. bakar's `-f` is `--manifest` and always has been; the
short `-f` that selects a flag belongs to `bitbake-getvar` inside the container,
not to `bakar getvar`.

## Output streams

The resolved value goes to **stdout**. Every diagnostic - kas progress banners,
container chatter, error text, the failure phase label - goes to **stderr**.
Command substitution and stderr suppression therefore both work as expected:

```bash
v=$(bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml)
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml 2>/dev/null
```

`--history` follows the same split: the `file:line` locations (or the
`no history recorded` line) go to stdout, while the
`<VAR> history (include-chain order):` heading is a diagnostic and goes to
stderr, so a piped `--history` run yields a clean list of locations.

With `--json`, stdout carries exactly one JSON document - the success document
on a successful query, the ERROR document on a failed one - and nothing else.

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

`--unexpanded` passes the `-u` flag to `bitbake-getvar`, which prints the value
before `${...}` references are substituted. This is helpful when you want to see
the literal assignment rather than the fully resolved string:

```bash
bakar getvar WORKDIR -f imx-6.12.49-2.2.0.xml --unexpanded
# prints something like: ${TMPDIR}/work/${MULTIMACH_TARGET_SYS}/${PN}/${EXTENDPE}${PV}-${PR}
```

### Variable flags

A variable flag (`VAR[FLAG]` in BitBake metadata) is queried either with the
`--flag` option or with the inline bracket spelling. Both normalise to the same
`bitbake-getvar -f FLAG VAR` call inside the container:

```bash
bakar getvar do_compile --flag noexec -f imx-6.12.49-2.2.0.xml -r busybox
bakar getvar 'do_compile[noexec]' -f imx-6.12.49-2.2.0.xml -r busybox
```

Quote the inline form. `[` and `]` are glob characters in fish and zsh, and an
unquoted `do_compile[noexec]` is either rewritten or rejected by the shell
before `bakar` ever sees it. bash leaves a non-matching pattern alone, so the
unquoted form happens to work there - quote it anyway so the command stays
portable.

Supplying `--flag` and the inline form at once exits 2, as does a malformed
bracket expression such as `VAR[` or `[FLAG]`. `--flag` cannot be combined with
`--history`: `bitbake -e` records assignments to the variable itself and carries
no per-flag history, so that combination exits 2 rather than silently answering
for the bare variable name.

An unset flag on a set variable is not an error - it prints an empty value and
exits 0, exactly like an unset variable.

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

`--json` emits a single JSON document on stdout. The shape depends on the mode
and on whether the query ran:

**Success, without `--history`:**

```text
var     string  variable name
value   string  resolved value (empty string when the variable or flag is unset)
flag    string  flag name (present only when --flag or the inline VAR[FLAG] form was used)
recipe  string  recipe name (present only when --recipe was given)
```

**Success, with `--history`:**

```text
var      string        variable name
history  array[string] ordered file:line source locations (empty array when no history)
recipe   string        recipe name (present only when --recipe was given)
```

**ERROR document** (emitted when the underlying `bitbake-getvar` or `bitbake -e`
call fails; `bakar` exits with that call's exit code):

```text
var     string  variable name
flag    string  flag name (present only when a flag was queried)
recipe  string  recipe name (present only when --recipe was given)
phase   string  one of checkout, parse, build, undetermined
error   string  the verbatim captured failure text, unwrapped and unformatted
```

`phase` attributes the failure to a stage of the kas run: `checkout` means the
repos named by the manifest could not be fetched or pinned, `parse` means the
metadata failed to parse, `build` means a task failed, and `undetermined` means
no known signature matched - bakar never guesses a phase.

The ERROR document has no `value` or `history` key; the presence of `error`
distinguishes it. The same failure text and phase label are also written to
stderr in plain form, so a non-JSON caller sees the full error verbatim.

```bash
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml --json | jq -r '.phase // "ok"'
```

## kas-container requirement

`getvar` always runs inside kas-container. The workspace must be synced
(`bakar sync`) and the container image must be available. `--history` additionally
requires bitbake to parse the full recipe environment, which can take longer
than a plain `bitbake-getvar` call.

Without `--history`, `getvar` invokes `bitbake-getvar --value --ignore-undefined`.
Both options require **bitbake 2.6.0 or newer**. The built-in NXP and TI
defaults (scarthgap, walnascar) ship bitbake 2.8 and are fine; a bring-your-own
kas YAML pinned to an older release such as kirkstone (bitbake 2.0) is not. On
an older bitbake the call fails with an
`unrecognized arguments: --ignore-undefined` error on stderr and `getvar`
forwards that exit code; use `bakar shell` and run `bitbake-getvar` by hand on
such a workspace.

`getvar` is read-only: it does not modify the build directory, sstate, or any
workspace files.

## Exit codes

Exit 0 means the query ran. A non-zero exit is reserved for a query that could
not run - it never means "the variable is unset".

| Code | Meaning |
|------|---------|
| 0 | The query ran. Covers a resolved value, an unset variable, and an unset flag; the latter two print an empty line and an empty `value` in `--json`. `--history` with no history comments also exits 0 with "no history recorded" |
| 2 | The query could not be formed or dispatched: no workspace found and no `--workspace` given, `--flag` combined with the inline `VAR[FLAG]` form, a malformed bracket expression, or `--flag` combined with `--history` |
| other | The underlying `bitbake-getvar` or `bitbake -e` call failed; its exit code is forwarded and the verbatim failure text plus a phase label go to stderr |

## Examples

```bash
# Resolve MACHINE globally
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml

# Resolve IMAGE_INSTALL within core-image-minimal's parse context
bakar getvar IMAGE_INSTALL -f imx-6.12.49-2.2.0.xml --recipe core-image-minimal

# Show the unexpanded (pre-substitution) value of WORKDIR for a recipe
bakar getvar WORKDIR -f imx-6.12.49-2.2.0.xml --recipe busybox --unexpanded

# Read a variable flag, long-option form
bakar getvar do_compile --flag noexec -f imx-6.12.49-2.2.0.xml --recipe busybox

# Same query, inline form - quote it, the brackets are globs in fish and zsh
bakar getvar 'do_compile[noexec]' -f imx-6.12.49-2.2.0.xml --recipe busybox

# Capture the value in a shell variable - diagnostics stay on stderr
machine=$(bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml)

# Silence kas progress chatter and keep only the value
bakar getvar MACHINE -f imx-6.12.49-2.2.0.xml 2>/dev/null

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
