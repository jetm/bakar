# bakar lock

Pin every floating layer revision to an exact commit for reproducible builds.

## Synopsis

```text
bakar lock [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--output` | `-o` | Write the pinned manifest here (NXP only; default: `<bsp_root>/pinned-manifest.xml`) |
| `--workspace` | `-w` | Workspace root override |

## Examples

```bash
# Lock NXP manifest to current HEAD SHAs
bakar lock -f imx-6.12.49-2.2.0.xml

# Lock to a custom output path
bakar lock -f imx-6.12.49-2.2.0.xml -o ~/manifests/pinned-20260601.xml

# Lock a BYO / TI / bbsetup project (produces kas-project.lock.yml)
bakar lock my-project.yml
bakar lock -f processor-sdk-10.1.0.8-config_var1.txt

```

## Output

**NXP** (repo manifest): writes a pinned `manifest.xml` using `repo manifest -r`. Default output is `<bsp_root>/pinned-manifest.xml`.

**BYO / TI / bbsetup**: runs `kas lock` (or `kas-container lock`) and writes `kas-project.lock.yml` in the workspace.

## Notes

- `lock` uses an ephemeral run directory so it does not pollute `build/runs/` with a bogus entry that `triage` and `report` would surface.
- After locking, pass the pinned manifest (or lockfile) to `bakar build` to reproduce the exact source state:

```bash
bakar lock -f imx-6.12.49-2.2.0.xml -o pinned.xml
bakar build -f pinned.xml -m imx8mp-var-dart
```

## See also

- [diff.md](diff.md) - compare two manifest versions to see what changed
- [layers.md](layers.md) - inspect current layer SHAs before locking
