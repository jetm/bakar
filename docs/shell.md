# bakar shell

Drop into a `kas shell` for the project, or run a single command inside it. The
shell runs on the host by default; opt into `kas-container shell` with the
global `--container` flag.

## Synopsis

```text
bakar shell [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--command` | `-c` | Run a single command instead of an interactive shell |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |

`--container` (route the shell through `kas-container` instead of the default host `kas shell`) is a **global** option, so it goes *before* the subcommand: `bakar --container shell ...`. Placing it after `shell` is rejected by the CLI parser. The older `--host` flag is a retained no-op alias for the already-default host path.

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

# Host mode - no Docker, runs kas shell directly (--host is global: before the subcommand)
bakar --host shell -f imx-6.12.49-2.2.0.xml -c "bitbake -c cleansstate glibc"
```

## Notes

- Positional YAML and `--manifest` are mutually exclusive.
- `bakar shell -c` is gated on the build directory's bitbake lock. It refuses to start (exit code 1) while a peer node's ownership marker or a live local build holds that lock. A peer-held or unattributable lock names the marker file and tells you how to clear it by hand when no build runs on any node; a live local build names its pid (or the container label in parentheses for a container build). A marker file that cannot be read or written is refused with the marker path and the errno.
- For a `-c` run, `bakar shell` writes the ownership marker and removes it when the command ends, but only when the bitbake lock is absent.
- The interactive shell (no `-c`) is not gated and writes no marker, and neither does the `devshell` task started through `bakar bitbake`. An unbounded session would otherwise hold the marker for its whole lifetime.
- Shell sessions are recorded in `<bsp_root>/build/runs/<ts>/` like builds; use `bakar log` to inspect them.

## See also

- [build.md](build.md) - full build pipeline
- [prefetch.md](prefetch.md) - runs `bitbake --runall=fetch` inside the kas environment
