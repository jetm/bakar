# bakar sstate-seed

Populate and inspect the native/cross sstate seed.

Seeding the native toolchain measured 26.4 min to 9.0 min on a `core-image-minimal` build (65% of wall-clock), the largest single build-time lever recorded on this fleet. The seed holds only native, cross, and crosssdk sstate objects - the ones `sstate.bbclass` prefixes with `${NATIVELSBSTRING}` - which makes it target-independent (one seed serves every MACHINE and image) but release-dependent (native hashes move with the oe-core revision). The seed is keyed by oe-core release codename so a seed built for the wrong release is inert rather than wrong: `--status` is worth running even when nothing is being populated, because an inert seed looks exactly like a configured one until someone compares build times.

## Synopsis

```text
bakar sstate-seed [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--status` | Report the seed for this workspace without writing anything |
| `--source` | sstate directory to populate from. Defaults to the configured `build.sstate_dir` |
| `--workspace` | Tree to read the oe-core release from. Use when the seed's owning checkout is not a bakar workspace |

## Examples

```bash
# Populate the seed for the current workspace's release from the configured sstate_dir
bakar sstate-seed

# Populate from an explicit sstate directory
bakar sstate-seed --source /mnt/yocto-cache/sstate

# Check whether a seed exists, which release it targets, and its SSTATE_MIRRORS line
bakar sstate-seed --status

# Read the release from a tree that carries oe-core but no bakar workspace markers
bakar sstate-seed --status --workspace /srv/benchmark-checkout
```

## Why `--workspace` exists

A seed belongs to an oe-core release, not to a bakar workspace, and the tree that owns one is not always a workspace bakar recognizes. A benchmark checkout can carry `openembedded-core/` and no workspace markers at all, yet still own the seed that pays off - `--workspace` names any tree containing `openembedded-core/meta/conf/layer.conf` directly, bypassing workspace detection.

## Status output

`--status` prints the resolved release, the seed directory, and - when a marker exists - the object count, size, and the source it was built from. A missing marker is reported as unrecorded rather than as an error: it is indistinguishable from a pre-marker seed or a corrupt one, and the only question that matters is that this seed cannot say what it was built for. When the marker's release does not match the workspace's, `--status` reports the seed as stale: it will never hit, and the build will quietly rebuild what it should have restored.

Every `--status` and populate run prints the `SSTATE_MIRRORS` line the seed needs to be consumed by a build.

## Applying the seed

`--status` prints the line but does not write it anywhere. Add it to your kas YAML or `~/.config/bakar/config.toml` (`build.sstate_mirrors`) yourself:

```bash
bakar sstate-seed --status
# ...
# SSTATE_MIRRORS line for this seed:
#   file://.* file:///mnt/yocto-cache/sstate/.native-seed/scarthgap/PATH;downloadfilename=PATH

bakar settings set build.sstate_mirrors 'file://.* file:///mnt/yocto-cache/sstate/.native-seed/scarthgap/PATH;downloadfilename=PATH'
```

## Notes

- The seed directory sits under the configured sstate directory at `.native-seed/<release>/` (or `.native-seed/_unknown/` when the release cannot be resolved).
- Populating overwrites on collision, so re-running after a pin bump refreshes stale objects instead of leaving them to shadow new ones.
- Populating from a workspace that has not built yet is reported distinctly from an empty-but-real source: a missing source directory is an error, while a source that exists but holds no native/cross objects returns a plain notice.
- `.siginfo` sidecars are copied alongside each object; a seed hit without its siginfo still costs a rebuild elsewhere via hash-equivalence.

## See also

- [settings.md](settings.md) - configure `build.sstate_dir` and `build.sstate_mirrors`
- [clean-cache.md](clean-cache.md) - prune stale sstate and ccache entries by age
- [insights.md](insights.md) - per-recipe sstate hit/miss analytics
