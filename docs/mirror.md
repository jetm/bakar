# bakar mirror

Seed a BitBake premirror tarball from an upstream git URL, entirely host-side.

Clones the repository bare-and-mirrored into a temporary directory, reads the last committer date, and packs a byte-stable `git2_*.tar.gz` tarball. No kas-container is involved and no manifest or kas YAML argument is needed.

## Synopsis

```text
bakar mirror GIT_URL [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--output-dir` | `-o` | Directory to write the tarball to |

## Examples

```bash
# Seed a tarball for meta-openembedded into the current dir or configured DL_DIR
bakar mirror https://github.com/openembedded/meta-openembedded.git

# Write the tarball to a specific directory
bakar mirror https://github.com/openembedded/meta-openembedded.git -o /tmp/dl
```

## Tarball naming

The output filename follows BitBake's `git2_*` premirror convention: `git2_<netloc><path>.tar.gz`, with every `/` and `:` in the URL's network location and path normalized to `.`. The scheme (`https://`) is dropped.

For example, `https://github.com/openembedded/meta-openembedded.git` produces:

```text
git2_github.com.openembedded.meta-openembedded.git.tar.gz
```

## Output directory precedence

The destination directory is resolved highest to lowest:

1. `--output-dir` / `-o` when supplied.
2. The configured `DL_DIR` (`build.dl_dir` in `~/.config/bakar/config.toml`), but only when set to a non-empty value and the command runs inside a workspace.
3. The current directory.

`DL_DIR` is frequently unset, so when it is `None` or empty the command falls through to the current directory.

## Byte-stability

The tarball is created with `tar --owner oe:0 --group oe:0 --mtime <committer-date>`, where the committer date comes from `git log --all -1 --format=%cD` in the bare clone. Pinning `--mtime` to the last committer date and forcing `oe:0` ownership makes the tarball byte-stable across re-runs of the same revision - a fresh `bakar mirror` of an unchanged upstream produces an identical file. The `oe:0` ownership matches what BitBake expects when unpacking a `git2_*` premirror tarball.

## Notes

- Requires `git` and `tar` on `PATH`.
- The temporary bare clone is removed after the tarball is written, including on failure.

## See also

- [prefetch.md](prefetch.md) - pre-fetch recipe sources into `DL_DIR`
- [settings.md](settings.md) - configure `build.dl_dir`
