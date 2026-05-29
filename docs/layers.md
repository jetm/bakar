# bakar layers

Print each synced layer's repo name, git short-hash, and branch. Read-only; never triggers a build, sync, or any workspace write.

## Synopsis

```text
bakar layers [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# List layers for the current workspace (auto-detected from cwd)
bakar layers

# List layers for a specific BSP manifest
bakar layers -f imx-6.12.49-2.2.0.xml

# List layers for a BYO kas YAML
bakar layers my-project.yml
```

## Output

```text
meta-imx         abc12345  main
meta-variscite   def67890  dunfell-var01
poky             11223344  dunfell
meta-openembedded 99aabbcc main
```

## Notes

- Layers are discovered by reading the kas YAML repos and checking git state of each cloned repo under `sources/`.
- When no layers are found (sources not synced yet), the command prints a hint and exits 0.
- `--show-layers` on `bakar build` and `bakar sync` calls the same logic automatically.
- Enable `layers.show_hashes = true` in `~/.config/bakar/config.toml` to always print hashes after every build and sync.

## See also

- [for-all.md](for-all.md) - run a command in every source repo
- [configuration.md](configuration.md) - `layers.show_hashes` setting
