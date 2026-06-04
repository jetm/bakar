# bakar presets

Named presets let you capture a full build configuration in `~/.config/bakar/config.toml` and invoke it with `bakar build --preset <name>`. Instead of retyping `--machine`, `--distro`, `--image`, `--manifest`, and `--branch` on every invocation, you write the values once and reference them by name. Multi-release presets extend this further: a single `--preset` invocation runs N sequential builds across Yocto releases and prints a summary table.

## Defining presets

Presets live in `~/.config/bakar/config.toml` as a TOML array-of-tables (`[[presets]]`). Each entry requires `name` and `family`. The supported families are `nxp`, `ti`, `generic`, and `bbsetup`.

### NXP single-release

```toml
[[presets]]
name = "imx8mp-scarthgap"
family = "nxp"
manifest = "imx-6.6.52-2.2.2.xml"
branch = "lf-6.6.y"
machine = "imx8mp-var-dart"
distro = "fsl-imx-xwayland"
image = "imx-image-full"
```

### NXP multi-release

Provide parallel lists of `manifests` and `branches` (same length):

```toml
[[presets]]
name = "imx8mp-all-releases"
family = "nxp"
manifests = ["imx-6.6.52-2.2.2.xml", "imx-6.12.49-2.2.0.xml"]
branches  = ["lf-6.6.y",             "lf-6.12.y"]
machine = "imx8mp-var-dart"
distro  = "fsl-imx-xwayland"
image   = "imx-image-full"
```

### bbsetup single-release

```toml
[[presets]]
name = "avocado-qemux86-64"
family = "bbsetup"
kas_yaml = "kas/avocado-qemux86-64.yml"
machine  = "qemux86-64"
image    = "avocado-os"
```

### bbsetup multi-release

Provide a list of kas YAML paths, one per release:

```toml
[[presets]]
name = "avocado-all-releases"
family = "bbsetup"
kas_yamls = ["kas/avocado-scarthgap.yml", "kas/avocado-styhead.yml"]
machine   = "qemux86-64"
image     = "avocado-os"
```

### generic (BYO kas YAML)

The `generic` family works identically to `bbsetup` but targets arbitrary kas YAML configurations not associated with a specific BSP toolchain:

```toml
[[presets]]
name = "my-bsp-dev"
family = "generic"
kas_yaml = "kas/my-custom-board.yml"
machine  = "my-board"
image    = "core-image-minimal"
```

### Optional fields

All presets accept two optional tuning fields:

| Field | Description |
|-------|-------------|
| `container_image` | Override the kas-container image for this preset |
| `tuning_overlay` | Path to an extra kas overlay appended for this preset |

### Field reference

| Field | Required for | Type | Notes |
|-------|-------------|------|-------|
| `name` | all | string | Unique identifier; used with `--preset` |
| `family` | all | string | One of `nxp`, `ti`, `generic`, `bbsetup` |
| `machine` | recommended | string | Target machine identifier |
| `distro` | nxp/ti | string | Distro (e.g. `fsl-imx-xwayland`) |
| `image` | recommended | string | Bitbake image target |
| `manifest` | nxp/ti single | string | Manifest filename (`.xml` or `.txt`) |
| `branch` | nxp/ti single | string | Source branch for the manifest |
| `manifests` | nxp/ti multi | list | One manifest per release |
| `branches` | nxp/ti multi | list | One branch per release; must match `manifests` length |
| `kas_yaml` | bbsetup/generic single | string | Path to kas YAML |
| `kas_yamls` | bbsetup/generic multi | list | One kas YAML path per release |
| `container_image` | - | string | Override kas-container image |
| `tuning_overlay` | - | string | Extra overlay path appended at build time |

Use either single-release fields (`manifest`/`kas_yaml`) or multi-release fields (`manifests`/`kas_yamls`) - not both.

## Precedence

Presets slot into the same resolution stack as every other config source. From highest to lowest priority:

```text
CLI flag
  BAKAR_* environment variable
    workspace .bakar.toml
      preset (--preset <name>)
        user ~/.config/bakar/config.toml
          built-in family default
```

Explicit flags always win. Passing `--image avocado-os-dev` alongside `--preset avocado-qemux86-64` uses `avocado-os-dev` for the image and the preset values for everything else:

```bash
bakar build --preset avocado-qemux86-64 --image avocado-os-dev
```

## Building with a preset

```bash
# Use a preset by name
bakar build --preset imx8mp-scarthgap

# Override one field; the preset supplies the rest
bakar build --preset imx8mp-scarthgap --image imx-image-minimal

# Dry-run: preview what would build without invoking kas-container
bakar build --preset imx8mp-scarthgap --dry-run

# bbsetup preset
bakar build --preset avocado-qemux86-64

# Multi-release fan-out (builds each release sequentially)
bakar build --preset imx8mp-all-releases
```

A missing preset name exits non-zero immediately with a clear error:

```bash
bakar build --preset does-not-exist
# Error: preset 'does-not-exist' not found
```

## Output paths

Each preset build writes into a composed output directory under `<bsp_root>/build/`:

| Family | Single-release path | Multi-release path |
|--------|--------------------|--------------------|
| nxp/ti | `<distro>-<machine>-<version>` | same per release (version from manifest) |
| bbsetup/generic | `<image>-<machine>` | `<image>-<machine>-<kas-yaml-stem>` |

For NXP, the version is extracted from the manifest filename: `imx-6.6.52-2.2.2.xml` becomes `6.6.52-2.2.2`. For bbsetup multi-release, the kas YAML stem distinguishes releases: `kas/avocado-scarthgap.yml` becomes `avocado-scarthgap`.

Non-preset builds use the existing `<bsp_root>/build/` layout unchanged.

## Multi-release builds

When a preset defines `manifests`/`branches` or `kas_yamls`, bakar builds each release sequentially, writes each into its own output directory, then prints a summary table:

```text
Release               Status   Duration
--------------------  -------  --------
imx-6.6.52-2.2.2.xml Success  14m 02s
imx-6.12.49-2.2.0.xml Failed  09m 47s

1 of 2 releases failed.
```

The exit code is 0 only when all releases succeed. Partial outputs from failed releases remain in their per-release directories for inspection.

## Managing presets

### `bakar presets list`

Print all presets from `config.toml` and `vendors.toml`:

```bash
bakar presets list
```

Output shows name and family for each preset. Prints "No presets defined." and exits 0 when none are configured.

### `bakar presets show <name>`

Print the full details of one preset including all fields and, for multi-release presets, each release:

```bash
bakar presets show imx8mp-all-releases
```

Exits non-zero if the named preset does not exist.

### `bakar presets add`

Interactive wizard to add a new preset to `~/.config/bakar/config.toml`. Requires an interactive terminal (stdin must be a TTY):

```bash
bakar presets add
```

The wizard selects the family first, then prompts for the family-appropriate fields:

- nxp/ti: manifest, branch, machine, distro, image
- bbsetup/generic: kas YAML path, machine, image

The new entry is written atomically to `config.toml`. Exits non-zero when stdin is not a TTY or the name already exists.

### `bakar presets remove <name>`

Remove a preset from `~/.config/bakar/config.toml`:

```bash
bakar presets remove imx8mp-scarthgap
```

Vendor-shipped presets (from `vendors.toml`) cannot be removed this way. Exits non-zero if the name is not found in `config.toml`.

## Shell completion

Shell completion for `--preset` is available after installing bakar's completion handler. Run the install command once for your shell:

```bash
bakar --install-completion
```

After reloading your shell (or opening a new terminal), `--preset` offers tab-completion from your defined presets:

```bash
bakar build --preset <TAB>
# imx8mp-scarthgap    imx8mp-all-releases    avocado-qemux86-64
```

Completion reads presets from `config.toml` and `vendors.toml` without triggering a source sync.

## See also

- [build.md](build.md) - full build pipeline options
- [configuration.md](configuration.md) - env vars and config.toml structure
- [config-reference.md](config-reference.md) - all config keys with types and defaults
- [settings.md](settings.md) - read and write config.toml defaults
