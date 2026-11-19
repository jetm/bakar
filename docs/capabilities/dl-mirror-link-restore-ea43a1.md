# `dl-mirror-link-restore`: a new requirement says a ready mirror directory that lists no regular files is reported as empty, by name and with the likely cause, and the exit code stays 0. No existing requirement of the capability changes, because its "nothing to do" outcome (0 to link, exit 0) stays true beside the new line

**Delivered by:** dl-mirror-link-hardening
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar cache link-mirror` (dry run) or `bakar cache link-mirror --apply`, and `bakar doctor --workspace ~/repos/work/peridio` (the row also runs in every `bakar build` pre-flight), on a host-mode host whose `[build] dl_mirror_paths` names a mirror directory that lists no regular files
Precondition: the bakar release carrying this change is installed; a host-mode build with `[build] dl_mirror_paths` set and a downloads directory configured through `DL_DIR` or `[build] dl_dir`; the mirror directory passes the readiness probe, meaning the path answers
Success signal: the verb's report has an `empty: <dir> (lists no regular files, as a share that is not mounted does)` line beside its usual counts and still exits 0 with 0 to link; the `dl-mirror-links` row shows a warning naming the directory when every listed directory is empty, and an informational result that names the empty directory when another directory holds files
Silent failure: a stale or partial mount that still lists some files is not reported as empty; svn, cvs and bzr tarballs stamped on the hub are linked without a checksum check, a run has no cap on links and a dangling link is never removed, and none of the three prints anything
