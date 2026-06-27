# bakar triage

Surface the last failed step of a build run with the relevant log tail and recipe log.

## Synopsis

```text
bakar triage [RUN_ID] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `RUN_ID` | | Run ID (`YYYYMMDD-HHMMSS`). Most recent failing run if omitted |
| `--run` | | Run ID to triage; alias for the positional argument and takes precedence |
| `--preset` | | Restrict run-dir selection to a preset (matches the preset build subdir name) |
| `--release` | | Restrict run-dir selection to a release (matches the version in the build subdir name) |
| `--kas-yaml` | `-k` | kas YAML for a BYO build (runs live next to it) |
| `--workspace` | `-w` | Workspace root override |
| `--json` | `-j` | Output the triage result as JSON instead of formatted text |

## Examples

```bash
# Triage the most recent failed build
bakar triage

# Triage a specific run by ID (positional or --run, equivalent)
bakar triage 20260601-143022
bakar triage --run 20260601-143022

# Under a multi-release preset fan-out, pick which run dir to triage
bakar triage --release 6.6
bakar triage --preset imx-multi

# Triage a BYO build
bakar triage --kas-yaml my-project.yml

# Triage a BYO build from a specific run
bakar triage 20260601-143022 --kas-yaml my-project.yml
```

## Output

When the run dir holds a `bitbake-events.json` artifact with recorded
failures, triage names each failing recipe and task and prints a tail of the
recorded task logfile (resolved from its container `/work/...` path to the
host path):

```text
:: triage 20260601-143022
✗ recipe linux-imx task do_compile failed
task log: .../work/imx8mp-poky-linux/linux-imx/.../temp/log.do_compile.1234
log.do_compile.1234 (tail):
  make[1]: *** [scripts/Makefile.build:480: drivers/net/wireless] Error 2
```

When no `bitbake-events.json` artifact is present, triage falls back to the
`kas.log` analysis:

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

When no failure is recorded on the fallback path:

```text
:: triage 20260601-150000
no step_fail events found
```

## How triage sources failure data

Triage is structured-failure-first. Every `bakar build`, `bakar bitbake`, and
`bakar clean-recipe` parses bitbake's persisted event log into a normalized
`bitbake-events.json` in the run directory. When that artifact is present and
its `failures[]` is non-empty, triage names the failing recipe/task directly
from it and prints a tail of the `logfile` each failure records - no `kas.log`
scraping. The recorded `logfile` is a container path (`/work/...`); triage
resolves it to a host path before reading it.

If `bitbake-events.json` is absent (run directories predating the event-log
capture, or a build interrupted before the artifact could be written), triage
falls back to the legacy path: it reads `error-report.json` written by
`kas_build` on failure, or, when that too is absent, parses `kas.log` live.
Both fallbacks reproduce the same output the structured path would have shown.

For the schema of `bitbake-events.json` and the raw `bitbake_eventlog.json` it
is parsed from, see [configuration.md](configuration.md#build-telemetry-directories).

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
- `build/<preset-subdir>/build/runs/` - per-release run dirs from a multi-release preset fan-out
- any other `*/build/runs/` subtree found within the workspace

When no `RUN_ID` is given and a single run dir candidate exists, that run is
used (today's single-build behavior). When several candidates exist - the
typical case under a multi-release preset fan-out - the default is the
most-recent run dir that recorded a failure. Override the default with `--run`
(or the positional run ID) for an exact run, or with `--preset`/`--release` to
restrict selection to the matching preset build subdir.

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
