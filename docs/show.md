# bakar show

Print the resolved build picture from local data only - no kas-container needed.

## Synopsis

```text
bakar show [OPTIONS]
```

## Description

`bakar show` reads the resolved `BuildConfig`, the tuning overlay stack, the
layer git state (`bblayers.conf`), and the cloned source repos from the local
workspace. It never starts kas-container or invokes bitbake. Five sections are
printed:

| Section | Content |
|---------|---------|
| **Config** | machine, distro, image, BSP family, container image, DL_DIR, SSTATE_DIR |
| **Overlays** | ordered list of `.yml` tuning overlays bakar would apply (base overlay first, then extras) |
| **Layers** | per-layer git short hash, branch, and version from `bblayers.conf` |
| **Sources** | cloned source repos discovered in the workspace |
| **Command** | exact `kas-container build` invocation a `bakar build` run would execute |

Layers and Sources appear empty when the workspace has not been synced yet.
Run `bakar sync` (or `bakar build`) first to populate them.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit a JSON document instead of formatted text |
| `--format` | | Output format: `text` (default) or `md` (Markdown headings) |

## Local-only guarantee

`bakar show` reads only local state: resolved Python config objects, overlay
filenames on disk, `bblayers.conf`, and source repo directories. It does not
pull any images, start any daemons, or write to the build directory or sstate.
It works on a freshly cloned workspace that has never been built.

## JSON output

`--json` emits a single JSON document with exactly these top-level keys:

```text
config    object  machine, distro, image, bsp_family, container_image, dl_dir, sstate_dir
overlays  array   overlay filenames in application order
layers    array   {repo, short_hash, branch, version} per layer
sources   array   {name, path} per cloned source repo
command   string  the full kas-container invocation string
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Report printed (workspace may be un-built; Layers/Sources will be empty) |
| 2 | No workspace found from the current directory and no `--workspace` given |

## Examples

```bash
# Default text output, manifest-driven NXP workspace
bakar show -f imx-6.12.49-2.2.0.xml

# Workspace not yet synced - Config, Overlays, and Command sections are populated;
# Layers and Sources show "(none - run bakar sync first)"
bakar show -f imx-6.12.49-2.2.0.xml

# Markdown output (one ## heading per section)
bakar show -f imx-6.12.49-2.2.0.xml --format md

# JSON document for scripting
bakar show -f imx-6.12.49-2.2.0.xml --json | jq .config

# Workspace root from a different directory
bakar show -f imx-6.12.49-2.2.0.xml --workspace /srv/bsp/nxp
```

## See also

- [build.md](build.md) - full build pipeline
- [sync.md](sync.md) - sync sources to populate Layers and Sources sections
- [layers.md](layers.md) - per-layer detail and project status via bitbake
- [configuration.md](configuration.md) - config.toml defaults that feed the Config section
