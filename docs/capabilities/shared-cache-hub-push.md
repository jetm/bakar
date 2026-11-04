# `shared-cache-hub-push`: finished downloads are pushed live from builders to PC1's store

**Delivered by:** shared-cache-hub-push
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar cache push --watch` on pc2 and pc3 as the user unit `bakar-cache-push.service`; `bakar cache status` and `bakar cache capture` by hand.
Precondition: `inotifywait`, `rsync` and `rrsync` on each host, nginx on PC1, the push key created on pc2 and pc3 with its public half in the cluster data, per-host `dl_dir`, `dl_mirror_url` and `cache_push_target`. Pushes land in a receiving directory (`downloads-incoming`) that PC1 never uses as its `DL_DIR`, because BitBake unpickles `.done` stamps; PC1 sets `dl_mirror_url` to its own nginx and consumes pushed files only through the mirror.
Success signal: a finished download appears with its `.done` in PC1's receiving directory within seconds, `bakar cache status` prints `queue: 0`, and another host logs a mirror hit instead of an upstream fetch.
Silent failure: the pusher stops or falls behind while builds keep passing, so other hosts quietly fetch from upstream; the `cache-push-service` doctor row fails on an inactive unit or a backlog over 60 minutes.
