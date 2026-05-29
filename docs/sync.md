# bakar sync

Run manifest-driven source sync without building. Equivalent to the first half of `bakar build`: doctor, then `repo init+sync` (NXP) or `oe-layertool populate` (TI), then setup-env.

## Synopsis

```text
bakar sync [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--machine` | `-m` | Target machine |
| `--distro` | `-d` | Distro |
| `--image` | `-i` | Image target |
| `--manifest` | `-f` | Manifest filename (NXP `.xml` or TI `.txt`) |
| `--branch` | `-b` | Branch override |
| `--skip-doctor` | | Skip pre-flight checks |
| `--clean` | | Remove `<bsp>/build/` before syncing |
| `--show-layers` | | Print layer git hashes after sync |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Sync NXP sources to a new manifest version
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Sync and confirm which commits landed
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --show-layers

# Sync TI sources
bakar sync -f processor-sdk-10.1.0.8-config_var1.txt -m am62x-var-som

# Force a clean re-sync (wipe build/ first)
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --clean
```

## Notes

- bitbake-setup workspaces are initialized externally via `bitbake-setup init`; `bakar sync` exits 2 for them.
- bakar detects manifest drift (wrong manifest, wrong branch, SHA drift) and forces a full re-sync when it detects it. Pass `--skip-doctor` to suppress the pre-flight gate, but not the drift check.

## See also

- [build.md](build.md) - full pipeline including sync
- [layers.md](layers.md) - inspect layer hashes after sync
