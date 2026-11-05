# Package feed

This feature publishes built packages as a feed that clients can resolve against. The commands, snapshots and layout are described in [feed.md](../feed.md).

## What your host needs

1. Use the Python interpreter bakar runs under. `bakar feed doctor` reports it for information only, because bakar declares `requires-python >=3.14` and an older interpreter fails at install time.
2. Install `createrepo_c`. It is a hard requirement with no fallback: without it a sync fails partway through. Fedora or RHEL: `dnf install createrepo_c`. Debian or Ubuntu: `apt install createrepo-c`. Arch: `pacman -S createrepo_c` (or the AUR). openSUSE: `zypper install createrepo_c`. No macOS package exists, so build a feed on Linux or in a Linux container.
3. Install `bash` and `tar` through your package manager. The staging script is a bash script and staging is a tar pipe.
4. Run `bakar feed doctor` to confirm. It runs with or without a kas YAML and exits `1` when any check blocks.

## Enable

Interactive: run `bakar init` and select the feed feature.

Non-interactive: `bakar init --feature feed --set build.feed_shared=true --set build.feed_dir=/srv/bakar/feed`

The feature writes these keys, and `--set` accepts only these for it:

- `build.feed_shared` (share the feed; default `true`)
- `build.feed_dir` (feed directory)

`build.feed_dir` is honored verbatim and takes precedence. `build.feed_shared` renders one feed under `~/.local/share/bakar/feed` for every workspace. Without either, each workspace renders into `<workspace>/_feed`.

A build that publishes a feed cannot be dispatched with `--on`. The catalog warns about this when feed is selected together with remote-dispatch.

## Turn off

Run `bakar init --disable feed`. It unsets `build.feed_shared` and `build.feed_dir` in your user config. It does not delete any rendered feed or snapshot.

## See also

- [feed.md](../feed.md)
- [config-reference.md](../config-reference.md)
- [remote-dispatch.md](remote-dispatch.md)
- `bakar feed doctor`
