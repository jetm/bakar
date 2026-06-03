# bakar layers

Print each synced layer's repo name, git short-hash, and branch. Extend with
two sub-verbs - `inspect` and `status` - for deeper per-layer and project-level
information via bitbake.

## Synopsis

```text
bakar layers [OPTIONS]
bakar layers inspect [KAS_YAML] [OPTIONS]
bakar layers status [KAS_YAML] [OPTIONS]
```

## Bare listing

`bakar layers` (no sub-verb) reads layer git state from the local workspace.
No kas-container is started.

### Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |

### Examples

```bash
# List layers for the current workspace (auto-detected from cwd)
bakar layers

# List layers for a specific BSP manifest
bakar layers -f imx-6.12.49-2.2.0.xml

# List layers for a BYO kas YAML
bakar layers my-project.yml
```

### Output

```text
meta-imx         abc12345  main
meta-variscite   def67890  dunfell-var01
poky             11223344  dunfell
meta-openembedded 99aabbcc main
```

Each row is: layer repo name, git short-hash, and branch.

### Notes

- Layers are discovered by reading the kas YAML repos and checking git state of
  each cloned repo under `sources/`.
- When no layers are found (sources not synced yet), the command prints a hint
  and exits 0.
- `--show-layers` on `bakar build` and `bakar sync` calls the same logic
  automatically.
- Enable `layers.show_hashes = true` in `~/.config/bakar/config.toml` to always
  print hashes after every build and sync.

## bakar layers inspect

Per-layer report: name, path, priority, LAYERSERIES_COMPAT, LAYERVERSION, and
what the layer provides.

Priority is read from the local `layer.conf` first, then overridden with the
authoritative value from `bitbake-layers show-layers` when the container run
succeeds.

**Container-backed:** requires a synced workspace and a working kas-container
image. Run `bakar sync` (or `bakar build`) before using this sub-verb.

### Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit a JSON array instead of human-readable text |

### Examples

```bash
# Inspect layers for an NXP manifest-driven workspace
bakar layers inspect -f imx-6.12.49-2.2.0.xml

# JSON output for scripting
bakar layers inspect -f imx-6.12.49-2.2.0.xml --json | jq '.[] | select(.name == "meta-imx")'

# BYO kas YAML
bakar layers inspect my-project.yml

# Explicit workspace root
bakar layers inspect -f imx-6.12.49-2.2.0.xml -w /srv/bsp/nxp
```

### Output

```text
meta-imx
  path:     /work/sources/meta-imx/meta-imx
  priority: 6
  compat:   scarthgap whinsy
  version:  1

meta-variscite
  path:     /work/sources/meta-variscite
  priority: 8
  compat:   scarthgap
  version:  1

poky
  path:     /work/sources/poky/meta
  priority: 5
  compat:   scarthgap
```

### JSON output

`--json` emits a JSON array. Each element is an object with these fields:

```text
name      string  layer name
path      string  absolute path to the layer directory
priority  string  BBFILE_PRIORITY value (empty string if not set)
compat    string  LAYERSERIES_COMPAT value (space-separated release names)
version   string  LAYERVERSION value (empty string if not set)
provides  string  recipes or package groups the layer provides (when available)
```

## bakar layers status

Project-level build summary: effective MACHINE, DISTRO, DISTRO_CODENAME,
BB_NUMBER_THREADS, PARALLEL_MAKE, SOURCE_MIRROR_URL, SSTATE_MIRRORS, and the
hashserv URL. Values are resolved by running `bitbake-getvar` for each variable
inside kas-container.

**Container-backed:** requires a synced workspace and a working kas-container
image. Run `bakar sync` (or `bakar build`) before using this sub-verb.

### Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch (NXP `.xml` or TI `.txt`) |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit a JSON object instead of human-readable text |

### Examples

```bash
# Show project-level status for a manifest-driven workspace
bakar layers status -f imx-6.12.49-2.2.0.xml

# JSON output for scripting
bakar layers status -f imx-6.12.49-2.2.0.xml --json | jq .machine

# BYO kas YAML
bakar layers status my-project.yml

# Explicit workspace root
bakar layers status -f imx-6.12.49-2.2.0.xml -w /srv/bsp/nxp
```

### Output

```text
status:
  MACHINE:           imx8mp-lpddr4-evk
  DISTRO:            fsl-imx-xwayland
  DISTRO_CODENAME:   scarthgap
  BB_NUMBER_THREADS: 16
  PARALLEL_MAKE:     -j16
  SOURCE_MIRROR_URL: not set
  SSTATE_MIRRORS:    not configured
  hashserv:          not configured
```

### JSON output

`--json` emits a JSON object with these fields:

```text
machine                  string       MACHINE value
distro                   string       DISTRO value
distro_codename          string       DISTRO_CODENAME value
bb_number_threads        string       BB_NUMBER_THREADS value
parallel_make            string       PARALLEL_MAKE value
source_mirror_url        string|null  SOURCE_MIRROR_URL value, null when not set
sstate_mirrors_configured bool        true when SSTATE_MIRRORS is non-empty
hashserv_url             string|null  BB_HASHSERV value, null when not configured
```

## Container requirement

`bakar layers inspect` and `bakar layers status` run
`bitbake-layers show-layers` and `bitbake-getvar` inside the kas-container image. Both
sub-verbs require:

- A synced workspace (`sources/` or `layers/` populated via `bakar sync` or
  `bakar build`).
- A working kas-container image pulled on the host (`kas-container` in PATH).

The bare `bakar layers` listing (git short-hash + branch) reads local git state
only and does not require kas-container or a synced workspace.

## See also

- [show.md](show.md) - local-only resolved build picture (no container needed)
- [build.md](build.md) - full build pipeline
- [sync.md](sync.md) - sync sources before running inspect or status
- [for-all.md](for-all.md) - run a command in every source repo
- [configuration.md](configuration.md) - `layers.show_hashes` setting
- [shell.md](shell.md) - drop into the container to run bitbake tooling manually
