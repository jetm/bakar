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

## Examples

```bash
# Run all checks (auto-detect workspace from cwd)
bakar doctor

# Run checks for a specific BSP
bakar doctor -f imx-6.12.49-2.2.0.xml
bakar doctor my-project.yml
```

## Check categories

Checks cover:

- Container runtime (Docker daemon version >= 20.10, storage driver, kas-container image present)
- Host tools (`repo`, `kas-container`, `git`, global git identity)
- Disk space (build root partition, ccache fill ratio)
- CPU threads (resolved `NPROC` and the bitbake settings it drives: task threads, parse threads, `make -j`; flags `local.conf` assignments that override the NPROC-derived values)
- Workspace filesystem (rejects vfat/exfat/ntfs/9p/nfs; sstate hardlinks need a local fs)
- Kernel sysctls (`fs.inotify.max_user_instances`, `fs.inotify.max_user_watches`)
- Kas YAML syntax (`kas dump` parse check)
- BSP-specific checks (repo manifest validity for NXP)
- PSI pressure support (kernel feature check, threshold calibration)
- Persistent hashserv daemon (when `[build] hashserv = true` — PID + TCP probe; see [hashserv.md](hashserv.md))
- sstate hash leak (host-specific variables that corrupt sstate task signatures)

## sstate hash-leak check

The `sstate-hash-leak` check scans `build/conf/local.conf` (plus sibling
conf-includes and active overlays) for assignments of host-specific variables
that leak into bitbake task signatures and break sstate reuse across builds and
hosts.

Variables scanned:

| Variable | Why it leaks |
|----------|--------------|
| `DATETIME` | changes on every build |
| `BUILD_REPRODUCIBLE_BINARIES` | host-dependent reproducibility flag |
| `PWD` | absolute build path varies per checkout |
| `USER` | varies per developer |
| `HOME` | varies per developer |
| `HOSTNAME` | varies per machine |

Severity is **WARN**, never BLOCK. It scans config text, not real signatures,
so it advises rather than stopping a build. The check reads host-side files, so
it runs in host mode too. It is skipped (no finding) when
`build/conf/local.conf` does not exist yet (pre-sync).

For each variable assigned without a matching `[vardepsexclude]` annotation, the
finding's fix hint contains the exact remediation line:

```text
DATETIME[vardepsexclude] += "DATETIME"
```

Add that annotation in `local.conf` (or an overlay) so the variable does not
corrupt sstate hashes.

## PSI calibration

Set `psi_autocalibrate = true` under `[build]` in
`~/.config/bakar/config.toml`. `bakar build` then samples `/proc/pressure`
during every build and writes the recommended `pressure_max_*` back to the
config afterwards, reporting what changed. The first build bootstraps the
values; later builds only raise a threshold (from an unthrottled
measurement), never lower it, so a light sstate-cached build cannot
over-throttle the next cold one. Delete the `pressure_max_*` keys to
recalibrate from scratch. See [configuration.md](configuration.md).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed (or only WARN/INFO findings) |
| 2 | At least one BLOCK-severity check failed |

## See also

- [build.md](build.md) - doctor runs automatically before every build
- [configuration.md](configuration.md) - `build.doctor` flag to disable auto-doctor
- [hashserv.md](hashserv.md) - what `check_hashserv` actually probes and how to fix its findings
