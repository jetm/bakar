# bakar triage

Surface the last failed step of a build run with the relevant log tail and recipe log.

## Synopsis

```text
bakar triage [RUN_ID] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `RUN_ID` | | Run ID (`YYYYMMDD-HHMMSS-PID`). Most recent failing run if omitted |
| `--run` | | Run ID to triage; alias for the positional argument and takes precedence |
| `--preset` | | Restrict run-dir selection to a preset (matches the preset build subdir name) |
| `--release` | | Restrict run-dir selection to a release (matches the version in the build subdir name) |
| `--kas-yaml` | `-k` | kas YAML for a BYO build (runs live next to it) |
| `--workspace` | `-w` | Workspace root override |
| `--json` | `-j` | Output the triage result as JSON instead of formatted text |
| `--on` | | Triage the run on this host over ssh instead of locally. Cannot be combined with `--workspace` |

## Examples

```bash
# Triage the most recent failed build
bakar triage

# Triage a specific run by ID (positional or --run, equivalent)
bakar triage 20260601-143022-48213
bakar triage --run 20260601-143022-48213

# Under a multi-release preset fan-out, pick which run dir to triage
bakar triage --release 6.6
bakar triage --preset imx-multi

# Triage a BYO build
bakar triage --kas-yaml my-project.yml

# Triage a BYO build from a specific run
bakar triage 20260601-143022-48213 --kas-yaml my-project.yml

# Triage a run dispatched with --on pc2 --detach, from any directory
bakar triage 20260601-143022-48213 --on pc2 --json
```

## Output

When the run dir holds a `bitbake-events.json` artifact with recorded
failures, triage names each failing recipe and task and prints a tail of the
recorded task logfile (resolved from its container `/work/...` path to the
host path):

```text
:: triage 20260601-143022-48213
✗ recipe linux-imx task do_compile failed
task log: .../work/imx8mp-poky-linux/linux-imx/.../temp/log.do_compile.1234
log.do_compile.1234 (tail):
  make[1]: *** [scripts/Makefile.build:480: drivers/net/wireless] Error 2
```

When no `bitbake-events.json` artifact is present, triage falls back to the
`kas.log` analysis:

```text
:: triage 20260601-143022-48213
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
:: triage 20260601-150000-51877
no step_fail events found
```

With `--json`, triage prints one document with these keys: `version`,
`run_id`, `failing_step`, `fail_reason`, `recipe_log` and `suggestions`.
`fail_reason` is the same text as the `failure.error` of `bakar status` for the
run: the captured error line, else `exit_code=N`. For a run ended by
`bakar stop` it is `stopped by bakar stop` and `failing_step` is `stopped`. For
a build the pre-flight checks refused it is the doctor text, with
`failing_step` `doctor` on the same line.
For a structured task failure it is null, because the failing recipe and task
already name the cause. A run that carries a stop request but recorded neither
a failing step nor a non-zero terminal event reports no failure here, although
`bakar status` can still derive `failed` for it from the unit's exit sentinel.
The text form prints the reason as recorded, square brackets included.

## How triage sources failure data

Triage is structured-failure-first. Every `bakar build`, `bakar bitbake`, and
`bakar clean-recipe` parses bitbake's persisted event log into a normalized
`bitbake-events.json` in the run directory. When that artifact is present and
its `failures[]` is non-empty, triage names the failing recipe/task directly
from it and prints a tail of the `logfile` each failure records - no `kas.log`
scraping. The recorded `logfile` is a container path (`/work/...`); triage
resolves it to a host path before reading it.

The failing step event also carries the first error line the build printed.
Triage reports it as the reason, falling back to `exit_code=N` when the build
printed none.

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

### Registered runs resolve from anywhere

A run started by `bakar build --detach` (or `--on HOST --detach`) is registered
with its run directory. When `RUN_ID` is given and the workspace lookup finds
nothing, triage uses the registered run directory directly, so it works from any
directory and does not need a BSP workspace. The workspace lookup still wins
when it finds the run. Without a `RUN_ID`, or with an id that is neither found
nor registered, behaviour is unchanged, including the `Not inside a BSP
workspace` error outside a workspace.

### Triage on a remote host

`--on HOST` runs `bakar triage [--json] [--preset P] [--release R] -- RUN_ID` on
the host over ssh and mirrors its stdout and exit status; remote stderr is
printed locally. `--preset` and `--release` are forwarded. A host beginning with
`-`, `--workspace` with `--on`, and `--kas-yaml` with `--on` (it names a local
path) are refused with exit 2 before any ssh call. A bakar that is missing or
whose version differs from the local one is a refusal (exit 2), and an
unreachable host (ssh exit status 255) also exits 2. A build of the same version
with a different content hash prints a warning on stderr and continues. The run
id is quoted and follows `--`, so it reaches the remote shell as a single word
that cannot be read as a flag.

### Unit handles

`RUN_ID` may also be a detached unit name (`bakar-detach-...`). Triage resolves
it to the unit's latest registered run, the same lookup `bakar status` uses, and
prints `bakar triage: unit <unit> -> latest registered run <run id>` on stderr so
the pick is visible. A unit with no registered run yet exits 1.

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

- Run IDs come from the timestamps bakar assigns at build start (`YYYYMMDD-HHMMSS-PID`, where `PID` is the process id of the bakar run). The timestamp is UTC on every host, whatever its time zone. Use `bakar log` to see what files are in a run directory.
- Run directories created by earlier versions are named in the host's local time. They are neither renamed nor rewritten, and each still resolves by its own ID. On a host east of UTC, the pick of the newest run can return a run from before the upgrade for up to the host's offset from UTC after the upgrade, because the old local-time name reads as later than the new UTC name.
- Suggestions are printed only when a pattern matches; a clean kas.log prints none.

## See also

- [build.md](build.md) - link printed by build on failure: `bakar triage <run_id>`
- [log.md](log.md) - tail run log files directly
- [report.md](report.md) - summary for successful builds
