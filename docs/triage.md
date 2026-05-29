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

## Notes

- Without `--kas-yaml`, triage searches both `nxp/build/runs/` and `ti/build/runs/` under the workspace.
- Run IDs come from the timestamps bakar assigns at build start (`YYYYMMDD-HHMMSS`). Use `bakar log` to see what files are in a run directory.
- Suggestions are pattern-matched from the kas.log tail (sstate corruption, missing host tools, disk full, etc.).

## See also

- [build.md](build.md) - link printed by build on failure: `bakar triage <run_id>`
- [log.md](log.md) - tail run log files directly
- [report.md](report.md) - summary for successful builds
