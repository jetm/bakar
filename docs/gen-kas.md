# bakar gen-kas

Regenerate the kas YAML from the current manifest or bitbake-setup config without building.

The output is topology-only (repos and layer paths). The BSP tuning block lives in the static overlay at `overlays/bakar-tuning-<bsp>.yml` and is applied at build time by `bakar build`.

## Synopsis

```text
bakar gen-kas [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--machine` | `-m` | Target machine |
| `--distro` | `-d` | Distro |
| `--image` | `-i` | Image target |
| `--manifest` | `-f` | Manifest filename |
| `--branch` | `-b` | Branch override |
| `--output` | `-o` | Output path (default: `<bsp_root>/kas-<bsp>.yml`) |
| `--dry-run` | `-n` | Print the resolved output and source paths, then exit without writing |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Regenerate NXP kas YAML from current manifest and bblayers.conf
bakar gen-kas -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Write to a custom path
bakar gen-kas -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart -o /tmp/inspect.yml

# Regenerate kas YAML for a bitbake-setup workspace (from config-upstream.json)
bakar gen-kas -w ~/bsp/my-bbsetup-ws
```

## Notes

- The generated YAML captures the manifest-to-repos mapping. It is not a standalone build config; `bakar build` layers the tuning overlay on top at run time.
- For bitbake-setup workspaces, `gen-kas` translates `config/config-upstream.json` into `kas-bbsetup.yml`. Run this to inspect what `bakar build` would use.

## See also

- [build.md](build.md) - uses the generated YAML as part of the full pipeline
- [dump.md](dump.md) - flatten the kas YAML plus overlay into a single resolved file
