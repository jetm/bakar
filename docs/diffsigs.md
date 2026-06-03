# bakar diffsigs

Show why a task missed sstate and rebuilt by comparing the current and prior task signatures.

## Synopsis

```text
bakar diffsigs <recipe> <task> [OPTIONS]
```

## How it works

`diffsigs` runs two sequential steps inside kas-container:

1. **printdiff** - `bitbake -S printdiff <recipe>` generates fresh sigdata for the recipe using bitbake's `printdiff` signature handler. This writes per-task `.sigdata` stamp files under `build/tmp/stamps/`.
2. **diffsigs** - `bitbake-diffsigs -t <recipe> <task>` compares the newly generated sigdata against the prior build's sigdata and renders the per-variable old-vs-new differences.

The rendered diff text is printed directly - `bitbake-diffsigs` output is already human-readable and lists each variable that changed the task hash, with the old and new values side by side.

When no prior sigdata exists (no prior build has run for this recipe), the second step fails and `diffsigs` exits non-zero with a message explaining that a prior build is required. It does not print an empty diff as success.

## Prior-build requirement

`diffsigs` needs at least one completed prior build for the recipe and task combination. The prior build writes the reference `.sigdata` files that `bitbake-diffsigs` compares against. If those files are absent:

```text
Required sigdata for busybox:do_compile does not exist.
Run a build first so bitbake writes the reference sigdata stamps,
then re-run: bakar diffsigs
```

Run `bakar build` for the workspace first, then re-run `diffsigs` after a subsequent change triggers a rebuild.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--machine` | `-m` | Override the target machine |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Find out why busybox do_compile rebuilt
bakar diffsigs busybox do_compile -f imx-6.12.49-2.2.0.xml

# Investigate a kernel task rebuild (machine override)
bakar diffsigs linux-imx do_compile -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# BYO kas YAML workspace - run from the workspace directory
bakar diffsigs busybox do_fetch

# Explicit workspace root from an unrelated directory
bakar diffsigs busybox do_compile -f imx-6.12.49-2.2.0.xml -w /path/to/workspace
```

## Output

```text
diffsigs: busybox do_compile

basehash changed from 7a3e91f2... to d4b2c083...

Variable WORKDIR changed:
  old: /builds/tmp/work/cortexa53-poky-linux/busybox/1.36.1-r0
  new: /builds/tmp/work/cortexa53-poky-linux/busybox/1.37.0-r0

Variable SRC_URI[md5sum] changed:
  old: 4b2b68c83d...
  new: 9f1a034d21...
```

## Notes

- Both steps run inside kas-container - a synced workspace and a working container image are required.
- The `printdiff` step may take several minutes for complex recipes because bitbake parses the full recipe environment before computing signatures.
- Run logs for both steps are written to `<bsp_root>/build/runs/<YYYYMMDD-HHMMSS>/` as `diffsigs-printdiff.log` and `diffsigs-render.log`. Use `bakar log` to inspect them.
- `diffsigs` is read-only with respect to deploy artifacts and sstate. The `.sigdata` stamps written by the `printdiff` step are normal bitbake build state, not mutations of existing build outputs.

## See also

- [build.md](build.md) - run the build that generates the reference sigdata
- [triage.md](triage.md) - surface the failing step and recipe log after a build failure
- [shell.md](shell.md) - drop into the container to run `bitbake-diffsigs` manually
- [log.md](log.md) - tail the diffsigs run logs directly
