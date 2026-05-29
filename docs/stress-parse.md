# bakar stress-parse

Stress-test the bitbake parser fork race with configurable parallelism and iteration count.

Used to reproduce and measure the parser race condition described in the bakar roadmap. Runs `bitbake -p <target>` N times and collects the failure rate.

## Synopsis

```text
bakar stress-parse [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--runs` | `-n` | Number of bitbake `-p` iterations (default: 10) |
| `--target` | | bitbake parse target (default: `world`, parses every recipe in the layer graph) |
| `--parse-threads` | | Override `BB_NUMBER_PARSE_THREADS` |
| `--machine` | `-m` | Machine override |
| `--image` | `-i` | Image override |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--branch` | `-b` | Branch override |
| `--host` | | Run plain `kas shell` on the host instead of `kas-container` |
| `--label` | | Free-form tag written into `summary.json` for cross-run aggregation |
| `--python` | | Path to a Python binary to use as bitbake's interpreter |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Run 10 parse iterations (default)
bakar stress-parse -f imx-6.12.49-2.2.0.xml

# Run 50 iterations for a reliable failure-rate estimate
bakar stress-parse -f imx-6.12.49-2.2.0.xml -n 50

# Parse a specific target instead of world
bakar stress-parse -f imx-6.12.49-2.2.0.xml --target linux-imx

# Set explicit parse thread count
bakar stress-parse -f imx-6.12.49-2.2.0.xml --parse-threads 8 -n 20

# Label a run for later aggregation
bakar stress-parse -f imx-6.12.49-2.2.0.xml -n 50 --label baseline

# Test a locally-built CPython for fork-safety
bakar stress-parse -f imx-6.12.49-2.2.0.xml --python ~/src/cpython/build/python
```

## Output

Prints a summary table at the end with pass/fail counts and average parse time:

```text
              stress-parse summary (nxp)
 metric       value
 ──────────── ────────────────────────────────────────
 runs         50
 passed       47
 failed       3
 avg seconds  18.3
 override     applied branch=scarthgap sha=abc1234
```

Exits 1 when any iteration failed; matched failure signatures are printed after the table (up to 10).

## Notes

- Diagnostic/research tooling; not part of the normal build workflow.
- Each run invokes kas-container (or kas in host mode), so total wall time is `N * parse_time`.
- The failure rate correlates with `BB_NUMBER_PARSE_THREADS` and kernel preempt model.
- The workspace must already be synced (`bakar build` or `bakar sync` first).
- Per-iteration logs and `summary.json` are written to `<bsp>/build/runs/<run-id>/stress-parse/`.

## See also

- [build.md](build.md) - normal build pipeline
- [doctor.md](doctor.md) - pre-flight checks
