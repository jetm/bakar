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

A hit becomes a symbolic link in the local downloads directory, so no copy is made. A directory that is not mounted is skipped and the fetch falls back to the http store, then to upstream. The push watcher ignores these links, so they are never pushed back to the store.

`bakar doctor` runs `dl-mirror-paths-ready` and warns when a listed directory is not ready.

Downloads already on disk that the store holds are not removed automatically. Run `bakar cache prune-downloads` to list what can be reclaimed; it removes nothing unless you add `--apply`.

## Turn off

Run `bakar init --disable dl-mirror`. It unsets `build.dl_mirror_url` in your user config. It does not touch the store or any file already downloaded.

## See also

- [hub-download-mirror.md](../capabilities/hub-download-mirror.md)
- [download-cache-push.md](../capabilities/download-cache-push.md)
- [config-reference.md](../config-reference.md)
- [doctor.md](../doctor.md)
- `bakar doctor`
