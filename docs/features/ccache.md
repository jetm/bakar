# Compiler cache

ccache reuses compiled objects across builds. `bakar init` can turn it on and write the settings for you.

## What your host needs

1. Install the `ccache` package. This is a hard requirement. Use your distro package manager, for example `sudo pacman -S ccache` or `sudo apt install ccache`.
2. Run `bakar doctor` and read the `ccache-health` check. It is a soft requirement: a warning does not block enabling the feature. Fix what it names and re-run `bakar doctor`.

## Enable

Interactive: run `bakar init` and select the ccache feature.

Non-interactive: `bakar init --feature ccache --set build.ccache=true`

The feature writes these keys, and `--set` accepts only these for it:

- `build.ccache` (default `true`)
- `build.ccache_shared` (default `false`; share the cache across workspaces)
- `build.ccache_dir` (cache directory; no default)

For example: `bakar init --feature ccache --set build.ccache_shared=true --set build.ccache_dir=/srv/ccache`

## Turn off

Run `bakar init --disable ccache`. It unsets `build.ccache`, `build.ccache_shared` and `build.ccache_dir` in your user config. It does not uninstall the `ccache` package or delete the cache directory.

## See also

- `bakar doctor`
- [Hash equivalence server](hashserv.md)
