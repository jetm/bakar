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
- When something under `build/` cannot be deleted, `clean` prints `could not fully remove <target>: <path>: <error>` (plus `(+N more undeletable entries)` when several are left), exits 1, and does not go on to remove the generated kas YAML. It prints `removed <target>` and exits 0 only when nothing was left behind, so a rerun after fixing the cause (permissions, a busy mount) that finds a clean tree exits 0. `bakar build --clean` stops on the same failure instead of building on a half-deleted tree.
- Before removing anything, `clean` checks who holds the build lock. It refuses with exit 1 when a peer node owns the build marker or when a build is running on this node, and the message names the marker and the manual clear (delete the marker and the `bitbake.lock` beside it by hand once no build runs on any node). When the lock cannot be attributed to a host, `clean` prints a warning and proceeds, because `clean` is the recovery path from an unreadable lock. There is no force flag: the refusal stands until the peer build finishes or you clear the marker yourself. `bakar build --clean` applies the same check.
- With `--all`, the generated `kas-<bsp>.yml` is also removed. The next `bakar build` will regenerate it from the manifest.
- BSP family is auto-detected from cwd by looking for `nxp/` or `ti/` subdirectories.
- `--all` stops the persistent hashserv daemon before the wipe **only when the daemon is keyed to this workspace** (`hashserv_state_key == bsp_root`, the no-shared-sstate fallback), so it gets the SIGTERM grace it needs to flush SQLite cleanly and its `<bsp_root>/.bakar/hashserv.db` is wiped with the rest of the workspace. When the daemon is keyed to a shared `SSTATE_DIR`, its database lives outside this build dir and sibling workspaces depend on it, so `--all` leaves it running and the wipe does not touch it. Use `bakar hashserv stop` to stop the daemon without dropping its database.

## See also

- [build.md](build.md) - `--clean` flag runs clean as part of the build pipeline
- [hashserv.md](hashserv.md) - what `--all` stops and the cache-wipe trade-off
