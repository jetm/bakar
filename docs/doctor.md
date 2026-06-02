# bakar doctor

Run every diagnostic check and report PASS/WARN/BLOCK status. Exits non-zero when any BLOCK-severity check fails.

## Synopsis

```text
bakar doctor [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |
| `--psi-calibrate` | `-C` | Monitor `/proc/pressure/` during a build and print recommended PSI thresholds |

## Examples

```bash
# Run all checks (auto-detect workspace from cwd)
bakar doctor

# Run checks for a specific BSP
bakar doctor -f imx-6.12.49-2.2.0.xml
bakar doctor my-project.yml

# Calibrate PSI pressure thresholds for config.toml
bakar doctor --psi-calibrate
# -> Start a build in another terminal, then Ctrl+C to print recommendations
```

## Check categories

Checks cover:

- Container runtime (Docker daemon version >= 20.10, storage driver, kas-container image present)
- Host tools (`repo`, `kas-container`, `git`, global git identity)
- Disk space (build root partition, ccache fill ratio)
- Workspace filesystem (rejects vfat/exfat/ntfs/9p/nfs; sstate hardlinks need a local fs)
- Kernel sysctls (`fs.inotify.max_user_instances`, `fs.inotify.max_user_watches`)
- Kas YAML syntax (`kas dump` parse check)
- BSP-specific checks (repo manifest validity for NXP)
- PSI pressure support (kernel feature check, threshold calibration)
- Persistent hashserv daemon (when `[build] hashserv = true` — PID + TCP probe; see [hashserv.md](hashserv.md))

## PSI calibration

`--psi-calibrate` reads `/proc/pressure/{cpu,io,memory}` every 500 ms while your build runs. When you press Ctrl+C it prints:

```text
Recommended [build] block for ~/.config/bakar/config.toml:
[build]
pressure_max_cpu = 72
pressure_max_io = 41
pressure_max_memory = 20
```

Copy those values into `~/.config/bakar/config.toml` to have bakar throttle bitbake task scheduling when system pressure exceeds the thresholds.

### Auto-calibration

To skip the manual `-C` run entirely, set `psi_autocalibrate = true` under
`[build]` in `~/.config/bakar/config.toml`. `bakar build` then samples
`/proc/pressure` during every build and writes the recommended `pressure_max_*`
back to the config afterwards, reporting what changed. The first (unthrottled)
build bootstraps the values; later builds only re-tune a dimension that was not
throttled, so the thresholds converge. See
[configuration.md](configuration.md).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed (or only WARN/INFO findings) |
| 2 | At least one BLOCK-severity check failed |

## See also

- [build.md](build.md) - doctor runs automatically before every build
- [configuration.md](configuration.md) - `build.doctor` flag to disable auto-doctor
- [hashserv.md](hashserv.md) - what `check_hashserv` actually probes and how to fix its findings
