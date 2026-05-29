# bakar prefetch

Pre-fetch all recipe sources into `DL_DIR` without running the build.

Runs `bitbake --runall=fetch <image>` inside the kas environment, populating the download cache ahead of an offline build.

## Synopsis

```text
bakar prefetch [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--machine` | `-m` | Target machine |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Pre-fetch all sources for the current NXP workspace
bakar prefetch -f imx-6.12.49-2.2.0.xml

# Pre-fetch with a specific machine
bakar prefetch -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Pre-fetch for a BYO build
bakar prefetch my-project.yml

# Pre-fetch TI sources
bakar prefetch -f processor-sdk-10.1.0.8-config_var1.txt -m am62x-var-som
```

## Notes

- Positional YAML and `--manifest` are mutually exclusive.
- The download cache path is controlled by `build.dl_dir` in `~/.config/bakar/config.toml` or the `DL_DIR` variable in your kas YAML.
- Useful before a conference, lab session, or air-gapped build where network access will be unavailable.

## Typical offline workflow

```bash
# 1. Pre-fetch while connected
bakar prefetch -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# 2. Transfer DL_DIR to the air-gapped machine

# 3. Point builds at the transferred cache
bakar settings set build.dl_dir /path/to/transferred/downloads

# 4. Build offline
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart
```

## See also

- [build.md](build.md) - full build pipeline
- [settings.md](settings.md) - configure `build.dl_dir` and `build.sstate_dir`
