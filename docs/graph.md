# bakar graph

Analyze a recipe's BitBake dependency graph and report structured insights.

## Synopsis

```text
bakar graph <recipe> [kas_yaml] [OPTIONS]
```

## Description

`bakar graph` runs `bitbake -g <recipe>` inside kas-container, retrieves the
emitted `task-depends.dot` and `pn-buildlist` artifacts, and analyzes them with
the pure `bakar.graph_analyze` module. The result is a structured view of the
recipe's build graph without requiring the user to read raw `.dot` files.

The reported insights are:

| Insight | Meaning |
|---------|---------|
| **package count** | Number of recipes in `pn-buildlist` (the full build list for the target) |
| **direct deps** | The target's immediate (first-level) dependencies (`direct deps:` in the text report; `direct_deps` in JSON) |
| **transitive deps** | Count of transitive descendants of the target in the PN-collapsed graph - how many recipes a change to it can affect (`transitive deps:` in the text report; `blast_radius` in JSON) |
| **longest build chain** | The longest dependency path through the graph (`networkx.dag_longest_path`) |
| **cycles** | The recipes forming a dependency cycle, or "no cycles" for an acyclic graph |
| **critical recipes** | The most depended-on recipes (highest in-degree) |

When a buildhistory `depends.dot` exists under `cfg.bsp_root/build/buildhistory/`,
an additional top-runtime-packages-by-fan-in section is appended; it is omitted
without error when absent.

For an unknown recipe or a failing `bitbake -g`, the command exits non-zero and
surfaces the bitbake error rather than printing empty graph data as success.

## kas-container requirement

All graph data comes from bitbake running inside kas-container. A synced
workspace with a working container image is required. Run `bakar sync` first if
the workspace has not been initialized.

The bitbake calls issued are:

1. `bitbake-getvar -r <recipe> TOPDIR` - resolve `${TOPDIR}` for this recipe
2. `bitbake -g <recipe>` - generate the dependency graph artifacts
3. `cat ${TOPDIR}/task-depends.dot` - retrieve the task-level dependency graph
4. `cat ${TOPDIR}/pn-buildlist` - retrieve the full build list

Run logs for each step are written to `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/`
as `graph-topdir.log`, `graph-bitbake-g.log`, `graph-task-depends.dot`, and
`graph-pn-buildlist.log`. Use `bakar log` to inspect them.

## ${TOPDIR} retrieval note

`bitbake -g` writes `task-depends.dot` and `pn-buildlist` into `${TOPDIR}`
inside the container. The command resolves `${TOPDIR}` via
`bitbake-getvar -r <recipe> TOPDIR` and then `cat`s the two files from that
directory, rather than reading a fixed host subpath. This keeps retrieval
family-agnostic: the bbsetup family's `bsp_root` is the workspace root, so no
fixed in-container build dir maps back to the host across NXP, TI, and bbsetup.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |
| `--format` | | Output format: `text` (default), `dot`, or `json` |
| `--depth` | | Bound transitive dependency expansion to N levels |

The positional `kas_yaml` selects the workspace for BYO/bbsetup families; NXP
and TI workspaces dispatch via the `-f` manifest instead.

## --format

| Value | Output |
|-------|--------|
| `text` | Default. Human-readable report: package count, direct deps, transitive deps, longest chain, cycle report, critical recipes |
| `dot` | The raw `task-depends.dot` text, unmodified |
| `json` | A machine-readable document carrying every insight (see below) |

## --depth

`--depth N` bounds the transitive dependency expansion to N levels. Without it,
the transitive-descendant count includes all descendants. The text report labels
the bounded count as `transitive deps (depth N)` (the `blast_radius` value in
JSON).

## JSON output

`--format json` emits a single JSON document. Top-level keys:

```text
target                string   the analyzed recipe name
depth                 int|null the --depth bound, or null when unbounded
package_count         int      number of recipes in pn-buildlist
direct_deps           array    the target's immediate (first-level) dependencies
blast_radius          int      transitive descendant count of the target
longest_chain         array    recipe names forming the longest build chain
cycle                 array    recipe names forming a cycle, empty when acyclic
critical              array    [name, in-degree] pairs, most depended-on first
top_runtime_packages  array    [name, fan-in] pairs; present only when a
                              buildhistory depends.dot was found
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Report printed successfully |
| 2 | No workspace found from the current directory and no `--workspace` given |
| other | bitbake exited non-zero (unknown recipe, `bitbake -g` failure, or container failure) |

## Examples

```bash
# Analyze busybox in an NXP workspace
bakar graph busybox -f imx-6.12.49-2.2.0.xml

# Machine-readable JSON for scripting
bakar graph busybox -f imx-6.12.49-2.2.0.xml --format json | jq .blast_radius

# Print the raw task-depends.dot
bakar graph busybox -f imx-6.12.49-2.2.0.xml --format dot

# Bound the blast-radius expansion to two levels
bakar graph core-image-minimal -f imx-6.12.49-2.2.0.xml --depth 2

# BYO kas YAML workspace - run from the workspace directory
bakar graph busybox

# Explicit workspace root from an unrelated directory
bakar graph busybox -f imx-6.12.49-2.2.0.xml -w /path/to/workspace
```

## See also

- [inspect.md](inspect.md) - deep per-recipe inspection report
- [bitbake.md](bitbake.md) - recipe-level build and task passthrough
- [getvar.md](getvar.md) - resolve a single variable with full assignment history
- [shell.md](shell.md) - drop into the container to run bitbake tooling directly
- [log.md](log.md) - tail the graph run logs
- [sync.md](sync.md) - sync sources before running container-backed commands
