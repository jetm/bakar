# Shared sstate and downloads

This feature points the sstate and downloads directories at shared locations, for example NFS exports, so several builders reuse the same objects. How `bakar doctor` probes those directories is described in [doctor.md](../doctor.md#nfs-cache-mount-readiness).

## What your host needs

1. Mount the shared sstate and downloads exports at the paths you will give `bakar init`. For NFS, use the `hard` or `softerr` mount option. Plain `soft` is flagged because its timeouts are indistinguishable from a failing disk.
2. Run `bakar doctor` and read the `cache-mounts` check. It is a hard requirement. It probes each configured directory with a 20-second deadline and blocks on an unresponsive share, or on a directory that `/etc/fstab` declares as NFS but that currently resolves to a local filesystem. Mount the shared directories and re-run `bakar doctor`.
3. Do not combine an HTTP sstate mirror with an `sstate_dir` on NFS. `bakar doctor` blocks that combination. Drop `build.sstate_mirror_url`, or keep `sstate_dir` on local disk.

## Enable

Interactive: run `bakar init` and select the nfs-cache feature.

Non-interactive: `bakar init --feature nfs-cache --set build.sstate_dir=/mnt/yocto-cache/sstate --set build.dl_dir=/mnt/yocto-cache/downloads`

The feature writes these keys, and `--set` accepts only these for it:

- `build.sstate_dir` (sstate directory; required)
- `build.dl_dir` (downloads directory; required)

## Turn off

Run `bakar init --disable nfs-cache`. It unsets `build.sstate_dir` and `build.dl_dir` in your user config. It does not unmount the shares or delete their contents.

## See also

- [doctor.md](../doctor.md#nfs-cache-mount-readiness)
- [config-reference.md](../config-reference.md)
- `bakar doctor`
