# bakar clean

Remove the BSP-specific `build/` directory to force a from-scratch build.

## Synopsis

```text
bakar clean [KAS_YAML] [OPTIONS]
```

## Arguments

| Argument | Description |
|----------|-------------|
| `KAS_YAML` | Optional BYO/meta-avocado kas YAML. When given, cleans that build dir (`workspace/build-<yaml-stem>/build`), mirroring `bakar build my.yml`, instead of an nxp/ti BSP dir. Mutually exclusive with `--bsp`/`--manifest`. |

## Options

| Flag | Description |
|------|-------------|
| `--all` | Also remove the generated kas YAML (kas-nxp.yml / kas-ti.yml) |
| `--bsp` | BSP family to clean: `nxp` or `ti`. Auto-detected from cwd when omitted |
| `--manifest`, `-f` | Manifest filename (back-compat alias for `--bsp`) |
| `--workspace`, `-w` | Workspace root override |

## Examples

```bash
# Clean NXP build directory (auto-detected from cwd)
bakar clean

# Clean explicitly specifying BSP family
bakar clean --bsp nxp
bakar clean --bsp ti

# Clean build/ and the generated kas YAML
bakar clean --bsp nxp --all

# Clean from outside the workspace
bakar clean --bsp nxp --workspace ~/bsp/my-workspace

# BYO/meta-avocado: clean workspace/build-<stem>/build for a kas YAML
bakar clean meta-avocado/kas/machine/qemuarm64.yml
```

## Notes

- `clean` removes `<bsp_root>/build/` which contains `tmp/`, `sstate-cache/`, `conf/`, and `runs/`. It does not remove `sources/` (synced layers).
- With `--all`, the generated `kas-<bsp>.yml` is also removed. The next `bakar build` will regenerate it from the manifest.
- BSP family is auto-detected from cwd by looking for `nxp/` or `ti/` subdirectories.
- `--all` stops the persistent hashserv daemon before the wipe **only when the daemon is keyed to this workspace** (`hashserv_state_key == bsp_root`, the no-shared-sstate fallback), so it gets the SIGTERM grace it needs to flush SQLite cleanly and its `<bsp_root>/.bakar/hashserv.db` is wiped with the rest of the workspace. When the daemon is keyed to a shared `SSTATE_DIR`, its database lives outside this build dir and sibling workspaces depend on it, so `--all` leaves it running and the wipe does not touch it. Use `bakar hashserv stop` to stop the daemon without dropping its database.

## See also

- [build.md](build.md) - `--clean` flag runs clean as part of the build pipeline
- [hashserv.md](hashserv.md) - what `--all` stops and the cache-wipe trade-off
