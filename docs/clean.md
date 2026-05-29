# bakar clean

Remove the BSP-specific `build/` directory to force a from-scratch build.

## Synopsis

```text
bakar clean [OPTIONS]
```

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
```

## Notes

- `clean` removes `<bsp_root>/build/` which contains `tmp/`, `sstate-cache/`, `conf/`, and `runs/`. It does not remove `sources/` (synced layers).
- With `--all`, the generated `kas-<bsp>.yml` is also removed. The next `bakar build` will regenerate it from the manifest.
- BSP family is auto-detected from cwd by looking for `nxp/` or `ti/` subdirectories.
- `--all` calls `hashserv.stop(bsp_root)` before the wipe so the persistent hashserv daemon (when running) gets the SIGTERM grace it needs to flush SQLite cleanly. The hash-equivalence database under `<bsp_root>/.bakar/hashserv.db` is removed together with the rest of the workspace - clean --all wipes the cache. Use `bakar hashserv stop` alone if you want to stop the daemon without dropping its database.

## See also

- [build.md](build.md) - `--clean` flag runs clean as part of the build pipeline
- [hashserv.md](hashserv.md) - what `--all` stops and the cache-wipe trade-off
