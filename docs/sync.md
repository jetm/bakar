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
| `--clean` | | Remove `<bsp>/build/` before syncing |
| `--show-layers` | | Print layer git hashes after sync |
| `--dry-run` | `-n` | Print the sync commands that would run, then exit without syncing |
| `--dry-run-script` | | Write a runnable bash script to PATH instead of syncing; use `-` for stdout. Distinct from `--dry-run`: this produces an executable script whose sync step matches the workspace family (repo for NXP, oe-layertool for TI, kas-container checkout for bbsetup). |
| `--workspace` | `-w` | Workspace root override |

**Global option:** `--hide-doctor-report`, placed before the subcommand
(`bakar --hide-doctor-report sync ...`), runs the doctor checks but prints
output only for build-blocking issues. Set `[build] show_doctor_report = false`
for the same effect on every invocation.

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

# Emit a runnable sync script to stdout without running the sync
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run-script -

# Write the sync script to a file
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run-script sync.sh
```

## Notes

- bitbake-setup workspaces are initialized externally via `bitbake-setup init`; `bakar sync` exits 2 for them.
- bakar detects manifest drift (wrong manifest, wrong branch, SHA drift) and forces a full re-sync when it detects it. The drift check is independent of the doctor pre-flight; `--hide-doctor-report` hides the doctor report but never suppresses the drift check.

## See also

- [build.md](build.md) - full pipeline including sync
- [layers.md](layers.md) - inspect layer hashes after sync
