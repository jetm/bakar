# bakar bitbake-override

Swap the BSP-bundled bitbake for a symlink to a local upstream checkout.

This is applied automatically by `bakar build`. The standalone command is useful for inspecting the current state, applying the override manually, or reverting it.

## Synopsis

```text
bakar bitbake-override [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--apply` | Apply the override (create symlink) |
| `--revert` | Remove the symlink; next `bakar build` restores the BSP bitbake via repo sync |
| `--status` | Print current override state and exit (default when no flag given) |
| `--branch` | Override branch in the upstream bitbake repo (default: `br-<major>.<minor>` auto-detected from the BSP bitbake) |
| `--repo` | Path to the upstream bitbake source repo (default: `~/repos/personal/yocto/bitbake` or `BAKAR_BITBAKE_OVERRIDE_REPO`) |
| `--manifest`, `-f` | Manifest filename for BSP family dispatch |
| `--workspace`, `-w` | Workspace root override |

## Examples

```bash
# Check current state
bakar bitbake-override
bakar bitbake-override --status

# Apply manually
bakar bitbake-override --apply

# Apply with a specific branch
bakar bitbake-override --apply --branch br-2.8

# Apply pointing at a different local repo
bakar bitbake-override --apply --repo ~/src/bitbake

# Revert (next build re-syncs to BSP bitbake)
bakar bitbake-override --revert

# Check state for TI (default dispatches to NXP without --manifest)
bakar bitbake-override --manifest processor-sdk-10.1.0.8-config_var1.txt
```

## Output

```text
bitbake-override: active branch=br-2.8 sha=abcdef01 upstream=2.8.3 bsp=2.8.1 (symlink active)
```

States: `active` (symlink present and points to a valid checkout), `stale` (symlink present but target moved), `disabled` (no override), `missing` (override expected but symlink broken).

## What path is swapped

| BSP family | Path |
|------------|------|
| NXP | `nxp/sources/poky/bitbake` |
| TI | `ti/sources/bitbake` |

## See also

- [build.md](build.md) - override is applied automatically at step 4 of every manifest-driven build
- [workspace.md](workspace.md) - BSP family dispatch
