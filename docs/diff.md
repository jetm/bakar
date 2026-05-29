# bakar diff

Compare two versions of a manifest or kas config and report per-layer SHA changes.

## Synopsis

```text
bakar diff OLD NEW [OPTIONS]
```

## Arguments

| Argument | Description |
|----------|-------------|
| `OLD` | Old manifest XML (NXP) or kas config (BYO/bbsetup) |
| `NEW` | New manifest XML (NXP) or kas config (BYO/bbsetup) |

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Compare two NXP manifest versions
bakar diff imx-6.6.52-2.2.0.xml imx-6.12.49-2.2.0.xml

# Compare with a pinned manifest (see what changed since pinning)
bakar diff pinned-manifest.xml imx-6.12.49-2.2.0.xml

# Compare two kas config files (delegates to kas diff / kas-container diff)
bakar diff kas-old.yml kas-new.yml
```

## Output (NXP manifest diff)

```text
meta-imx         abc12345  def67890  +23  changed
meta-variscite   11223344  11223344       unchanged
poky             99aabbcc  00112233  +8   changed
```

Columns: layer name, old SHA (8 chars), new SHA (8 chars), commit count ahead (`+N` if resolvable), change marker.

## Notes

- When both arguments are `.xml` files, bakar parses the manifests and diffs layer SHAs directly. The commit count requires the layer repos to be checked out under `sources/`.
- For non-`.xml` files, bakar delegates to `kas diff` (or `kas-container diff` outside host mode) and passes the exit code through.

## See also

- [lock.md](lock.md) - pin current SHAs for later diffing
- [layers.md](layers.md) - inspect current layer SHAs
