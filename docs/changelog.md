# bakar changelog

Generate release notes between two pinned workspace states.

## Synopsis

```text
bakar changelog FROM TO [OPTIONS]
```

## Arguments

| Argument | Description |
|----------|-------------|
| `FROM` | From-state: manifest XML path, kas lockfile path, or git ref |
| `TO` | To-state: manifest XML path, kas lockfile path, or git ref |

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--workspace` | `-w` | Workspace root override |
| `--format` | | Output format: `text` (default) or `md` (markdown) |

## Examples

```bash
# Compare two NXP manifest versions
bakar changelog imx-6.6.52-2.2.0.xml imx-6.6.52-2.2.2.xml -w ~/bsp/nxp

# Mix a manifest XML with a git ref (HEAD resolves from the workspace)
bakar changelog imx-6.6.52-2.2.0.xml HEAD -w ~/bsp/nxp

# Compare two kas lockfiles (BYO/bbsetup)
bakar changelog kas.lock.v1 kas.lock.v2 -w ~/bsp

# Markdown output (for embedding in release notes or issues)
bakar changelog imx-6.6.52-2.2.0.xml imx-6.6.52-2.2.2.xml --format md
```

## Output

Default text output, grouped by Added / Removed / Modified:

```text
Added:
  + meta-security (a1b2c3d4)

Removed:
  - meta-legacy (99aabbcc)

Modified:
  ~ meta-imx: abc12345..def67890 (7 commits)
      def67890 imx: fix DDR calibration timeout
      abc12346 imx: update device tree for i.MX8MP
```

Unchanged layers are omitted from all three sections. When there are no
changes, the output is:

```text
No changes between the two states.
```

With `--format md`, output starts with a heading naming the from/to states
and each section is a markdown subheading:

```text
## Changelog: imx-6.6.52-2.2.0.xml -> imx-6.6.52-2.2.2.xml

### Added

- **meta-security** (a1b2c3d4)

### Modified

- **meta-imx**: `abc12345..def67890` (7 commits)

  - `def67890 imx: fix DDR calibration timeout`
  - `abc12346 imx: update device tree for i.MX8MP`
```

## Notes

**Auto-detection of input format:**

Each positional argument is classified independently:

- **Manifest XML** - argument ends with `.xml`. Pins are read via
  `parse_manifest_pins`; each `<project>` element's `revision` attribute is
  the pinned commit. Applicable to NXP and TI workspaces.
- **Kas lockfile** - argument is a JSON file with a top-level `repos` key.
  Pins are read from each entry's `commit` field. Applicable to BYO/bbsetup
  workspaces.
- **Git ref** - anything that is not a `.xml` file path and not a JSON
  lockfile on disk. bakar runs `git show <ref>:<path>` under the workspace
  to find a manifest XML (in `.repo/manifests/`) or a `kas.lock` at that
  ref. Fails with exit code 2 if neither is found.

The two arguments can be different types. For example, `FROM` can be a
manifest XML and `TO` can be a git ref; bakar auto-detects each one
independently.

**Commit log excerpts:** for Modified layers, bakar runs
`git log --oneline <from-sha>..<to-sha>` in the checked-out source directory
(looked up under `sources/` then `layers/` inside the workspace root). When
the source directory is not present, the commit count and log lines are
omitted; only the SHA range is shown.

**Exit codes:**

- `0`: completed successfully
- `2`: a pin input could not be parsed or a git ref yielded no manifest or lockfile

## See also

- [diff.md](diff.md) - compare two manifest or kas config versions at the file level
- [drift.md](drift.md) - compare the current workspace checkout against its pinned state
- [lock.md](lock.md) - pin current SHAs to a lockfile
- [layers.md](layers.md) - inspect current layer SHAs
