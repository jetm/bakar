# Cache push

This feature sets `build.cache_push_target`, the ssh destination that `bakar cache push --watch` sends finished downloads to. A pusher host watches its downloads directory and pushes each finished download to the central store, so other hosts find it through the mirror instead of going upstream. This page covers the pusher host. For the store side, see [download-cache-push.md](../capabilities/download-cache-push.md), and for the command, [cache.md](../cache.md).

## What your host needs

1. Install `inotifywait`, which comes from the inotify-tools package (the distro's package manager provides it). It is a hard requirement. `bakar cache push --watch` exits 1 when it is missing.
2. Create an ssh keypair at `~/.ssh/bakar-cache-push` (no bakar doc describes how to generate it). It is a hard requirement. The pusher offers only this key (`IdentitiesOnly=yes`, `IdentityAgent=none`), so an agent or other identity is never used. Install the public half `~/.ssh/bakar-cache-push.pub` on the store host as a key restricted to write-only `rrsync`. To confirm which key authenticated, compare `ssh-keygen -lf ~/.ssh/bakar-cache-push.pub` with the fingerprints in the store host's `journalctl -u sshd | grep 'Accepted publickey'`.
3. Set `build.dl_dir`, the downloads directory the pusher watches. It is a hard requirement. Set it directly, for example `bakar settings set build.dl_dir <path>`, or through the [nfs-cache](nfs-cache.md) feature.
4. Enable the `bakar-cache-push.service` user unit: `systemctl --user enable --now bakar-cache-push.service`. This is advisory, not a hard requirement. `bakar doctor` runs the `cache-push-service` check, which warns when the unit is inactive or the oldest queued push is over 60 minutes old (over 15 minutes is INFO). `journalctl --user -u bakar-cache-push.service` shows why a queue is not draining, and `bakar cache status` prints the backlog.

The push target must be a separate receiving directory on the store host, never the store host's own `DL_DIR`. Leave `cache_push_target` unset on the store host.

## Enable

Interactive: run `bakar init` and select the cache-push feature.

Non-interactive: `bakar init --feature cache-push --set build.cache_push_target=pc1:/mnt/JETM_SATA_9.1T/yocto-cache/downloads-incoming`

The feature writes one key, and `--set` accepts only it for this feature:

- `build.cache_push_target` (cache push target, `user@host:/path`; required)

## Turn off

Run `bakar init --disable cache-push`. It unsets `build.cache_push_target` in your user config. It does not stop or disable the `bakar-cache-push.service` unit, remove the push key, or delete anything already pushed. Stop the unit yourself with `systemctl --user disable --now bakar-cache-push.service`.

## See also

- [download-cache-push.md](../capabilities/download-cache-push.md)
- [cache.md](../cache.md)
- [doctor.md](../doctor.md)
- [dl-mirror.md](dl-mirror.md)
- `bakar doctor`
