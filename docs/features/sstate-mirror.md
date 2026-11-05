# sstate mirror

This feature sets `build.sstate_mirror_url`. When it is set, bakar activates the shared-cache overlay, which wires `SSTATE_MIRRORS` to `<url>/all/PATH;downloadfilename=PATH`, so builds fetch prebuilt sstate objects from the mirror. See [config-reference.md](../config-reference.md) for the key and [doctor.md](../doctor.md) for the checks.

## What your host needs

1. A reachable HTTP mirror. Check it with `curl -sI <url>`, using the URL you will give `bakar init`. `bakar doctor` runs the `sstate-mirror-reachable` check, which is a hard requirement. It warns when the URL does not answer a 3-second HTTP probe, and then builds rebuild what the mirror would have supplied. Fix the URL or your network and re-run `bakar doctor`.
2. If you have no mirror of your own, `http://sstate.yoctoproject.org` works directly with no additional config: `curl -sI http://sstate.yoctoproject.org`.
3. Keep `sstate_dir` off NFS. The `sstate-mirror-shared-mount` check blocks an http(s) sstate mirror combined with an sstate directory on NFS. Use a `file://` mirror or move the sstate directory to local disk, then re-run `bakar doctor`.

## Enable

Interactive: run `bakar init` and select the sstate-mirror feature.

Non-interactive: `bakar init --feature sstate-mirror --set build.sstate_mirror_url=http://sstate.yoctoproject.org`

The feature writes one key, and `--set` accepts only it for this feature:

- `build.sstate_mirror_url` (sstate mirror URL; required)

## Turn off

Run `bakar init --disable sstate-mirror`. It unsets `build.sstate_mirror_url` in your user config. It does not touch the mirror or any sstate already downloaded.

## See also

- [doctor.md](../doctor.md)
- [config-reference.md](../config-reference.md)
- [nfs-cache.md](nfs-cache.md)
- `bakar doctor`
