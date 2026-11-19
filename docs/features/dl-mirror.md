# Downloads mirror

This feature sets `build.dl_mirror_url`, the base URL of a central downloads store. When it is set, bakar activates the hub-cache overlay, which serves http(s), ftp, crate and gomod sources from the store before going upstream. Git and gitsm sources are not served. The design is described in [hub-download-mirror.md](../capabilities/hub-download-mirror.md).

## What your host needs

1. A reachable store at the URL you will give `bakar init`. Check it with `curl -sI <url>`. `bakar doctor` runs the `dl-mirror-reachable` check, which is a hard requirement. It warns when the URL does not answer a 3-second HTTP probe, which ignores proxy environment variables.
2. Nothing else is required to build. When the store does not answer, the key is dropped for that build and builds fall back to upstream fetches. Fix the URL or the store and re-run `bakar doctor` to get the mirror back.

## Enable

Interactive: run `bakar init` and select the dl-mirror feature.

Non-interactive: `bakar init --feature dl-mirror --set build.dl_mirror_url=http://192.168.8.174:18742/downloads`

The feature writes one key, and `--set` accepts only it for this feature:

- `build.dl_mirror_url` (downloads mirror URL; required)

## Use the mounted store as a file mirror

The store can also be read as a local directory instead of over http. This applies to host-mode builds only; container builds do not use it.

What the host needs:

1. The store's read-only share mounted on the host.
2. `dl_mirror_paths` set to the mounted downloads directory first, then the incoming directory, as a whitespace-separated list.

With those set, bakar activates the hub-mount overlay, which puts each directory in front of upstream as a `file://` mirror for http(s), ftp, crate and gomod sources. Git and gitsm sources are not served.

A hit becomes a symbolic link in the local downloads directory, so no copy is made. A directory that is missing or does not answer a bounded `stat -f` is skipped and the fetch falls back to the http store, then to upstream. The probe checks that the path answers, not that a share is mounted on it, so an unmounted plain directory that exists passes and simply holds no files. The push watcher ignores these links, so they are never pushed back to the store.

`bakar doctor` runs `dl-mirror-paths-ready` and warns when a listed directory is not ready.

Downloads already on disk that the store holds are not removed automatically. Run `bakar cache prune-downloads` to list what can be reclaimed; it removes nothing unless you add `--apply`.

### Keep a local name for every hub file

A build directory whose `do_fetch` stamp is current skips the fetch, so a source that was fetched earlier is never looked up again. If its file is not on disk (for example after `bakar cache prune-downloads --apply` removed it because the hub holds it), the build fails at unpack. Keeping a link in the local downloads directory for every file the hub holds prevents that.

The `dl-mirror-links` doctor row creates the missing links itself at every build pre-flight and every `bakar doctor` run, so nothing needs to be done by hand. `bakar cache link-mirror` exists for a dry run (no `--apply`, nothing is created) and for a host that wants the links without starting a build. See [cache.md](../cache.md) for the verb.

Integrity trade-off: a linked file is not checked against a recipe while a stale stamp skips the fetch. BitBake still checks the checksum at any later fetch of that source.

Limits:

- `git` and `gitsm` sources are never linked. A build that lacks one needs a refetch of that recipe.
- A source that a fetcher stores in a subdirectory of the downloads directory is not linked; only files directly inside a mirror directory are.
- A dangling link is left as it is; the verb never replaces an existing entry.
- A hub that drops during a build stalls reads through the links like any other read through the mount (see the limit below).

### Known limit: a hub that drops during a build

The readiness probe only protects builds that start while the mount is down. If the hub stops answering NFS while a build is running, an access to a link or a mirror file blocks until the NFS timeout, and each source fetch can wait that long. Measured on pc2 with the hub's NFS port blocked: a cold `stat` and a one-file fetch both stalled past 40 s and 150 s, at `timeo=600` and at `timeo=50`, and a `stat` on the existing sstate mount stalled the same way. A hub outage already stalls a build through the sstate share, so the mirror joins that dependency and does not add a new one. To stop using the directories, remove `dl_mirror_paths` from your user config.

## Turn off

Run `bakar init --disable dl-mirror`. It unsets `build.dl_mirror_url` in your user config. It does not touch the store or any file already downloaded.

## See also

- [hub-download-mirror.md](../capabilities/hub-download-mirror.md)
- [download-cache-push.md](../capabilities/download-cache-push.md)
- [config-reference.md](../config-reference.md)
- [doctor.md](../doctor.md)
- `bakar doctor`
