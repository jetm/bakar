# bakar drift

Compare each source layer's pinned revision against its actual checked-out HEAD.

## Synopsis

```text
bakar drift [KAS_YAML] [OPTIONS]
```

## Arguments

| Argument | Description |
|----------|-------------|
| `KAS_YAML` | Kas config file (BYO/bbsetup workspaces). Omit when using `--manifest`. |

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename (NXP/TI). Use instead of `KAS_YAML` for NXP/TI workspaces. |
| `--workspace` | `-w` | Workspace root override |
| `--all` | | Include sources with no drift (clean repos) |
| `--json` | | Emit output as a JSON array |
| `--format` | | Output format: `text` (default) or `md` |

## Examples

```bash
# BYO/bbsetup: compare pinned lockfile SHAs against checked-out HEADs
bakar drift meta-avocado/kas/machine/qemux86-64.yml -w ~/bsp

# NXP: compare manifest-pinned SHAs against checked-out HEADs
bakar drift -f imx-6.6.52-2.2.2.xml -w ~/bsp/nxp

# Show all sources, including those with no drift
bakar drift meta-avocado/kas/machine/qemux86-64.yml --all

# Emit machine-readable JSON (parseable by jq)
bakar drift meta-avocado/kas/machine/qemux86-64.yml --json | jq '.[].source'

# Markdown output (for embedding in reports or issues)
bakar drift -f imx-6.6.52-2.2.2.xml --format md
```

## Output

Default text output, one row per drifted source:

```text
source           pinned    actual    distance
meta-imx         abc12345  def67890  +7
poky             11223344  55667788  +2
```

Columns: source name, pinned SHA (8 chars), actual HEAD SHA (8 chars), commit distance ahead of the pin.

With `--all`, clean sources appear with an empty distance column:

```text
source           pinned    actual    distance
meta-imx         abc12345  def67890  +7
meta-variscite   11223344  11223344
```

With `--json`, output is a JSON array. Each object contains:

```json
[
  {
    "source": "meta-imx",
    "pinned": "abc12345",
    "actual": "def67890",
    "distance": 7
  }
]
```

## Notes

**Pin reading by family:**

- **NXP/TI**: pin SHAs are read from the manifest XML (`--manifest/-f`) via `parse_manifest_pins`. Each `<project>` element's `revision` attribute is the pinned commit.
- **BYO/bbsetup**: pin SHAs are read from the kas lockfile (`kas.lock` or the output of `kas lock --format json`). The lockfile must contain a top-level `repos` key with per-repo `commit` fields. When no lockfile is present, bakar falls back to the git HEAD of each source directory, which means drift reports zero distance for every source - the output is accurate but uninformative without a lockfile.

**Drift computation:** for each source, bakar runs `git rev-parse HEAD` in the checked-out directory and `git rev-list <pinned>..<actual> --count` to measure how many commits the workspace has advanced past the pin. Only sources that have moved forward are reported by default.

**Exit codes:**

- `0`: completed successfully (zero drift or drift reported)
- `2`: the family could not be resolved, or the pin input (manifest/lockfile) is missing for the resolved family

## See also

- [diff.md](diff.md) - compare two manifest or kas config versions directly
- [lock.md](lock.md) - pin current SHAs to a lockfile
- [layers.md](layers.md) - inspect current layer SHAs without comparing to a pin
- [changelog.md](changelog.md) - generate release notes between two pinned states
