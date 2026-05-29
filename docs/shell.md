# bakar shell

Drop into a `kas-container shell` for the project, or run a single command inside it.

## Synopsis

```text
bakar shell [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--command` | `-c` | Run a single command instead of an interactive shell |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--host` | | Bypass kas-container, run plain `kas shell` on the host |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Interactive shell - manifest-driven NXP
bakar shell -f imx-6.12.49-2.2.0.xml

# Interactive shell - BYO kas YAML
bakar shell my-project.yml

# Run a single command: clean sstate for one recipe
bakar shell -f imx-6.12.49-2.2.0.xml -c "bitbake -c cleansstate linux-imx"

# Inspect the bitbake configuration inside the container
bakar shell -f imx-6.12.49-2.2.0.xml -c "bitbake -e | grep ^BB_TASKS_SCHEDULER"

# Investigate a parse failure (bitbake -p parses all recipes)
bakar shell -f imx-6.12.49-2.2.0.xml -c "bitbake -p 2>&1 | tail -30"

# Host mode - no Docker, runs kas shell directly
bakar shell -f imx-6.12.49-2.2.0.xml --host -c "bitbake -c cleansstate glibc"
```

## Notes

- Positional YAML and `--manifest` are mutually exclusive.
- Shell sessions are recorded in `<bsp_root>/build/runs/<ts>/` like builds; use `bakar log` to inspect them.

## See also

- [build.md](build.md) - full build pipeline
- [prefetch.md](prefetch.md) - runs `bitbake --runall=fetch` inside the kas environment
