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

By default `diffsigs` parses the `bitbake-diffsigs` output and renders a structured summary: the **root cause(s)** (the variables or dep-list change that moved the hash), the **rebuild chain** (how the requested task traces back to the root-cause task, possibly across recipes), and the **dependency list diff** (tasks added/removed from the signature). Pass `--raw` to print the full unprocessed `bitbake-diffsigs` text instead. When no structure can be extracted, the raw kas-stripped text is printed as a fallback.

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
| `--raw` | | Print the full unprocessed `bitbake-diffsigs` output (including kas startup lines) instead of the structured summary |
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

Default (structured) output:

```text
diffsigs: busybox do_compile

Root cause:
  WORKDIR changed
  (basehash changed independently — task function or referenced code changed)

Rebuild chain  (2 levels deep):
  busybox:do_compile  ← requested
    ↳ busybox:do_fetch  ← root cause

Dependency list diff  (1 added, 1 removed):
  + virtual/libc:do_populate_sysroot
  - virtual/libc-initial:do_populate_sysroot
```

With `--raw`, the full unprocessed `bitbake-diffsigs` output is printed instead
(`basehash changed from ... to ...`, `Variable X changed:` with old/new values),
including the kas startup lines.

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
