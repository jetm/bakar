# cache-mount-readiness: the bounded readiness probe for effective cache directories, the cache-mounts doctor check, cache-touching check gating, and the pre-launch gate in front of every bitbake invocation

**Delivered by:** nfs-cache-resilience
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar doctor <kas.yml>` run against a workspace whose `SSTATE_DIR`/`DL_DIR`/`CCACHE_DIR` resolve to NFS mounts (PC3's live config is the reference case); the same readiness/refusal logic is also exercised by `bakar build`, `bakar bitbake`, `bakar getvar`, and `bakar hashserv start`/`bakar prserv start`
Precondition: a workspace config with `sstate_dir`/`dl_dir`/`ccache_dir` pointing at NFS-backed paths; for the daemon-state check, `[build] hashserv = true` with no `bb_hashserve` configured
Success signal: exit code 0 with a `cache-mounts` PASS row when every share is responsive and correctly typed; exit code non-zero with a `cache-mounts` FAIL row at BLOCK severity naming the unresponsive or misclassified directory and its server when a share is dead, returned within the ~20s probe deadline rather than hanging; a `daemon-state` FAIL row naming `bb_hashserve`/`prserv_host` when the per-workspace daemon would land on NFS
Silent failure: none - every failure mode this change addresses (an unresponsive share, a share resolving to the wrong filesystem, network-backed daemon state, a doctor check that hangs) is required to produce a named row in the doctor report or a named CLI refusal, never a silent hang or an untraceable failure
