# `cache-hang-capture`: `bakar cache capture`, which gathers process, kernel, lock and NFS state for every running BitBake process and classifies the hang

**Delivered by:** shared-cache-hub-push
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar cache push --watch` on pc2 and pc3, run as the chezmoi-managed user unit `bakar-cache-push.service`; PC1 serves its downloads store with the user unit `bakar-store.service` (nginx on port 18742) and builds with `DL_DIR` pointing at the store directory; `bakar cache status` and `bakar cache capture` are run by hand on any host
Precondition: `inotifywait`, `rsync` and `rrsync` on each host (present on all three), nginx installed on PC1, the push key created on pc2 and pc3 and its public half pasted into `cluster.cache_push_keys` and applied on PC1, per-host local `dl_dir`, `dl_mirror_url` and `cache_push_target` in bakar's config, and the sstate mount re-mounted with NFSv3
Success signal: a finished download appears with its `.done` on PC1 within seconds, `bakar cache status` prints `queue: 0`, a build on another host logs a mirror hit instead of an upstream fetch, and `bakar cache capture` ends with a `summary:` line
Silent failure: the pusher stops or falls behind while builds keep passing, so other hosts quietly fetch from upstream; mitigated by the cache-push-service doctor row, which fails on an inactive unit and on a backlog older than 60 minutes
