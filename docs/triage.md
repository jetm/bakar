# bakar triage

Surface the last failed step of a build run with the relevant log tail and recipe log.

## Synopsis

```text
bakar triage [RUN_ID] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `RUN_ID` | | Run ID (`YYYYMMDD-HHMMSS`). Most recent run if omitted |
| `--kas-yaml` | `-k` | kas YAML for a BYO build (runs live next to it) |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Triage the most recent failed build
bakar triage

# Triage a specific run by ID
bakar triage 20260601-143022

# Triage a BYO build
bakar triage --kas-yaml my-project.yml

# Triage a BYO build from a specific run
bakar triage 20260601-143022 --kas-yaml my-project.yml
```

## Output

```text
:: triage 20260601-143022
✗ step kas_build failed: recipe failed

kas.log (tail):
  ERROR: linux-imx-6.12.29+git-r0 do_compile: ...
  ...

bitbake recipe log: .../work/imx8mp-poky-linux/linux-imx/.../temp/log.do_compile
log.do_compile (tail):
  make[1]: *** [scripts/Makefile.build:480: drivers/net/wireless] Error 2

suggestions:
  - check sstate-cache for a stale artifact: bitbake -c cleansstate linux-imx
```

When no `step_fail` events are found:

```text
:: triage 20260601-150000
no step_fail events found
```

## How triage sources failure data

When a build fails, bakar writes `error-report.json` into the run directory at
build time. On the next `bakar triage` call, triage reads that file directly -
no log re-scanning. If the file is absent (old run directories, or a build
interrupted before the report could be written), triage falls back to parsing
`kas.log` live, reproducing the same output.

The fast path is transparent: the output format is identical whether the report
comes from `error-report.json` or from a live log scan.

## error-report.json artifact

Location: `<run-dir>/error-report.json`

Written by `kas_build` on any non-zero exit, before the run directory is closed.
A passing build (exit code 0) leaves no `error-report.json`.

Keys:

| Key | Type | Description |
|-----|------|-------------|
| `step` | string | Always `"kas_build"` |
| `machine` | string | MACHINE value from the build config |
| `distro` | string | DISTRO value from the build config |
| `bsp_family` | string | One of `"nxp"`, `"ti"`, `"bbsetup"`, or the vendor family name |
| `exit_code` | integer | kas-container exit code |
| `kas_log_tail` | list of strings | Last 80 lines of `kas.log` |
| `recipe_errors` | list of objects | Each object has `recipe`, `task`, and `excerpt` keys |
| `suggestions` | list of strings | Pattern-matched hints from the kas.log tail |

Example:

```json
{
  "step": "kas_build",
  "machine": "imx8mp-var-dart",
  "distro": "fsl-imx-wayland",
  "bsp_family": "nxp",
  "exit_code": 1,
  "kas_log_tail": ["ERROR: linux-imx-6.12.29+git-r0 do_compile: ..."],
  "recipe_errors": [
    {"recipe": "linux-imx", "task": "do_compile", "excerpt": "make[1]: *** Error 2"}
  ],
  "suggestions": ["check sstate-cache for a stale artifact: bitbake -c cleansstate linux-imx"]
}
```

## Run directory discovery

`bakar triage` (without a `--kas-yaml` or explicit workspace path) searches the
workspace for run directories across all BSP families:

- `nxp/build/runs/` - NXP i.MX builds
- `ti/build/runs/` - TI Sitara builds
- `build/runs/` at the workspace root - BYO and bitbake-setup builds
- any other `*/build/runs/` subtree found within the workspace

Results are sorted most-recent-first by run ID. The most recent run across all
families is used when no `RUN_ID` is given.

## Suggestions

Triage pattern-matches the kas.log tail against a table of known failure modes.
Recognized patterns include:

- sstate corruption - suggests `cleansstate`
- missing host tools
- disk full
- compiler OOM-kill (`cc1plus: out of memory`, killed signal) - suggests lowering `BB_NUMBER_THREADS` / `PARALLEL_MAKE`
- GitHub rate-limit (`HTTP Error 429`, API rate limit exceeded) - suggests waiting or authenticating
- network/DNS failure (`Name or service not known`, `Temporary failure in name resolution`, `Connection timed out`)
- PREMIRROR connection failure (`Connection refused` during a mirror fetch)

## Notes

- Run IDs come from the timestamps bakar assigns at build start (`YYYYMMDD-HHMMSS`). Use `bakar log` to see what files are in a run directory.
- Suggestions are printed only when a pattern matches; a clean kas.log prints none.

## See also

- [build.md](build.md) - link printed by build on failure: `bakar triage <run_id>`
- [log.md](log.md) - tail run log files directly
- [report.md](report.md) - summary for successful builds
